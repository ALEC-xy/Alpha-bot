# Solana Alpha Call Bot - Ultimate Edition
# Alert types: Early Entry, High Conviction, Confirmed Play, Dead Resurrection
# Signals: organic volume, wallet integrity, farmer detection, co-buy network,
#          elite wallet entry, coordinated entry, stealth accumulation, bundle check

import os
import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from dotenv import load_dotenv
import httpx

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
ALERT_CHAT_ID = os.getenv("ALERT_CHAT_ID", "")
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else "https://api.mainnet-beta.solana.com"

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
RAYDIUM_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"

# ── State ─────────────────────────────────────────────────────────────────────

paused = False
alerted_tokens = set()
token_history = {}       # {mint: {snapshots, first_seen, trade_history}}
wallet_db = {}           # {wallet: WalletProfile}
cobuy_db = defaultdict(list)   # {frozenset(w1,w2): [mint,...]}
alert_log = []           # [{mint, type, score, mcap_entry, mcap_30m, ts}]

settings = {
    "mcap_min": 5000,
    "mcap_max": 20000,
    "mcap_dead_min": 2000,
    "mcap_dead_max": 10000,
    "mcap_confirmed_min": 20000,
    "mcap_confirmed_max": 100000,
    "bundle_max": 35,
    "min_quality_wallets": 2,
    "min_buy_sell_ratio": 0.65,
    "min_unique_buyers": 12,
    "volume_spike_pct": 150,
    "min_liquidity": 800,
    "confirmation_count": 3,
}


# ── RPC ───────────────────────────────────────────────────────────────────────

async def rpc(client, method, params):
    try:
        r = await client.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params
        }, timeout=20)
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        logger.warning(f"RPC {method}: {e}")
        return None


async def helius_txs(client, address, limit=20):
    if not HELIUS_API_KEY:
        return []
    try:
        r = await client.get(
            f"https://api.helius.xyz/v0/addresses/{address}/transactions",
            params={"api-key": HELIUS_API_KEY, "limit": limit},
            timeout=15
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


# ── Token data ────────────────────────────────────────────────────────────────

async def get_token_snapshot(client, mint):
    try:
        supply_result = await rpc(client, "getTokenSupply", [mint])
        supply = 0.0
        if supply_result:
            info = supply_result.get("value", {})
            ui = info.get("uiAmount")
            if ui:
                supply = float(ui)
            else:
                amount = int(info.get("amount", 0))
                decimals = int(info.get("decimals", 6))
                supply = amount / (10 ** decimals)

        price_r = await client.get(f"https://price.jup.ag/v6/price?ids={mint}", timeout=8)
        price = float(price_r.json().get("data", {}).get(mint, {}).get("price", 0) or 0)
        mcap = price * supply

        holders_r = await client.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": "snap",
            "method": "getTokenAccounts",
            "params": {"mint": mint, "limit": 50, "options": {"showZeroBalance": False}}
        }, timeout=20)
        holders_data = holders_r.json().get("result", {}).get("token_accounts", [])
        holder_count = len(holders_data)
        holders = [{"owner": h.get("owner"), "amount": float(h.get("amount", 0))} for h in holders_data if h.get("owner")]

        mint_r = await rpc(client, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        can_mint = can_freeze = False
        try:
            info = mint_r["data"]["parsed"]["info"]
            can_mint = bool(info.get("mintAuthority"))
            can_freeze = bool(info.get("freezeAuthority"))
        except Exception:
            pass

        return {
            "mint": mint,
            "supply": supply,
            "price": price,
            "mcap": mcap,
            "holder_count": holder_count,
            "holders": holders,
            "can_mint": can_mint,
            "can_freeze": can_freeze,
            "is_pump": mint.endswith("pump"),
            "ts": time.time(),
        }
    except Exception as e:
        logger.warning(f"snapshot {mint[:8]}: {e}")
        return None


async def get_recent_trades(client, mint, limit=40):
    txs = await helius_txs(client, mint, limit=limit)
    trades = []
    for tx in (txs or []):
        ttype = tx.get("type", "")
        is_buy = "BUY" in ttype or "SWAP" in ttype
        is_sell = "SELL" in ttype
        if is_buy or is_sell:
            trades.append({
                "wallet": tx.get("feePayer", ""),
                "type": "buy" if is_buy else "sell",
                "slot": tx.get("slot", 0),
                "ts": tx.get("timestamp", 0),
                "sol_amount": sum(abs(t.get("amount", 0)) for t in tx.get("nativeTransfers", [])),
            })
    return trades


# ── Organic volume scoring ────────────────────────────────────────────────────

def organic_volume_score(trades, holder_count):
    if not trades:
        return 0, []

    issues = []
    score = 100

    buys = [t for t in trades if t["type"] == "buy"]
    sells = [t for t in trades if t["type"] == "sell"]

    unique_buyers = set(t["wallet"] for t in buys)
    unique_sellers = set(t["wallet"] for t in sells)
    recyclers = unique_buyers & unique_sellers

    # Wash trading
    wash_ratio = len(recyclers) / max(len(unique_buyers), 1)
    if wash_ratio > 0.3:
        score -= 30
        issues.append(f"wash trading ({len(recyclers)} recyclers)")

    # Same slot clustering
    slot_map = defaultdict(list)
    for t in buys:
        slot_map[t["slot"]].append(t["wallet"])
    coordinated = [wallets for wallets in slot_map.values() if len(wallets) >= 3]
    if len(coordinated) >= 2:
        score -= 25
        issues.append(f"coordinated buys ({len(coordinated)} slots)")

    # Tiny uniform buy sizes (copy trader bots)
    buy_amounts = [t["sol_amount"] for t in buys if t["sol_amount"] > 0]
    if buy_amounts:
        avg = sum(buy_amounts) / len(buy_amounts)
        tiny = sum(1 for a in buy_amounts if a < 0.05)
        if tiny / max(len(buy_amounts), 1) > 0.7:
            score -= 20
            issues.append("tiny uniform buys (copy bots)")

    # Holder count vs volume ratio
    if holder_count < 8 and len(trades) > 25:
        score -= 20
        issues.append("high volume low holders")

    # Buy/sell ratio
    buy_sell_ratio = len(buys) / max(len(trades), 1)
    if buy_sell_ratio < settings["min_buy_sell_ratio"]:
        score -= 15
        issues.append(f"low buy ratio ({buy_sell_ratio:.0%})")

    # Time distribution — organic volume spreads over time
    if len(buys) >= 5:
        timestamps = sorted(t["ts"] for t in buys if t["ts"])
        if timestamps:
            span = timestamps[-1] - timestamps[0]
            if span < 30:
                score -= 15
                issues.append("all buys in <30s window")

    return max(0, score), issues


# ── Wallet profiling ──────────────────────────────────────────────────────────

class WalletProfile:
    def __init__(self, address):
        self.address = address
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.hold_times = []
        self.tokens_traded = set()
        self.avg_buy_size = 0
        self.is_farmer = False
        self.farmer_score = 0
        self.quality_score = 50
        self.last_updated = 0

    @property
    def win_rate(self):
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.5

    @property
    def avg_hold_mins(self):
        return sum(self.hold_times) / len(self.hold_times) if self.hold_times else 0


async def profile_wallet(client, wallet):
    if wallet in wallet_db and time.time() - wallet_db[wallet].last_updated < 300:
        return wallet_db[wallet]

    profile = WalletProfile(wallet)

    txs = await helius_txs(client, wallet, limit=50)
    if not txs:
        wallet_db[wallet] = profile
        return profile

    profile.total_trades = len(txs)

    buy_times = {}
    sell_times = {}
    tokens_seen = set()

    for tx in txs:
        ttype = tx.get("type", "")
        ts = tx.get("timestamp", 0)
        for transfer in tx.get("tokenTransfers", []):
            mint = transfer.get("mint", "")
            if not mint:
                continue
            tokens_seen.add(mint)
            if "BUY" in ttype or "SWAP" in ttype:
                if mint not in buy_times:
                    buy_times[mint] = ts
            elif "SELL" in ttype:
                if mint not in sell_times:
                    sell_times[mint] = ts

    profile.tokens_traded = tokens_seen

    for mint in buy_times:
        if mint in sell_times and buy_times[mint] and sell_times[mint]:
            hold_mins = abs(sell_times[mint] - buy_times[mint]) / 60
            profile.hold_times.append(hold_mins)

    # Farmer detection
    farmer_score = 0
    if profile.avg_hold_mins < 2 and len(profile.hold_times) > 5:
        farmer_score += 40
    if profile.avg_hold_mins < 5 and len(profile.hold_times) > 10:
        farmer_score += 20
    if profile.total_trades > 100:
        farmer_score += 10

    profile.farmer_score = farmer_score
    profile.is_farmer = farmer_score >= 40

    # Quality scoring
    if profile.is_farmer:
        profile.quality_score = 20
    elif profile.avg_hold_mins > 15 and profile.total_trades >= 10:
        profile.quality_score = 80
    elif profile.avg_hold_mins > 5 and profile.total_trades >= 5:
        profile.quality_score = 65
    elif profile.total_trades >= 20:
        profile.quality_score = 60
    else:
        profile.quality_score = 40

    profile.last_updated = time.time()
    wallet_db[wallet] = profile
    return profile


async def get_quality_wallets(client, holders):
    if not holders:
        return [], 0

    top20 = holders[:20]
    profiles = await asyncio.gather(*[profile_wallet(client, h["owner"]) for h in top20])

    quality = []
    for h, p in zip(top20, profiles):
        if p and p.quality_score >= 60 and not p.is_farmer:
            quality.append({"wallet": h["owner"], "amount": h["amount"], "profile": p})

    avg_quality = sum(p.quality_score for p in profiles if p) / max(len(profiles), 1)
    return quality, avg_quality


# ── Co-buy network ────────────────────────────────────────────────────────────

def check_cobuy_history(wallet_addrs):
    if len(wallet_addrs) < 2:
        return 0, []
    best_count = 0
    best_pair = []
    for i in range(len(wallet_addrs)):
        for j in range(i + 1, len(wallet_addrs)):
            pair = frozenset([wallet_addrs[i], wallet_addrs[j]])
            count = len(cobuy_db.get(pair, []))
            if count > best_count:
                best_count = count
                best_pair = [wallet_addrs[i], wallet_addrs[j]]
    return best_count, best_pair


def update_cobuy_db(wallet_addrs, mint):
    for i in range(len(wallet_addrs)):
        for j in range(i + 1, len(wallet_addrs)):
            pair = frozenset([wallet_addrs[i], wallet_addrs[j]])
            if mint not in cobuy_db[pair]:
                cobuy_db[pair].append(mint)


# ── Momentum ──────────────────────────────────────────────────────────────────

def update_momentum(mint, mcap, holders):
    now = time.time()
    if mint not in token_history:
        token_history[mint] = {
            "snapshots": [],
            "first_seen": now,
            "confirmations": 0,
        }

    h = token_history[mint]
    h["snapshots"].append({"ts": now, "mcap": mcap, "holders": holders})
    h["snapshots"] = h["snapshots"][-10:]

    age_mins = (now - h["first_seen"]) / 60
    snaps = h["snapshots"]

    if len(snaps) < 2:
        return 50, "neutral", age_mins

    old_mcap = snaps[0]["mcap"]
    old_holders = snaps[0]["holders"]
    mcap_change = (mcap - old_mcap) / max(old_mcap, 1) * 100
    holder_change = holders - old_holders

    score = 50
    if mcap_change > 30: score += 15
    if mcap_change > 80: score += 10
    if holder_change > 3: score += 10
    if age_mins >= 3 and mcap > old_mcap: score += 10
    if mcap_change < -10: score -= 25

    # Acceleration check — last 3 snapshots trending up
    if len(snaps) >= 3:
        recent = [s["mcap"] for s in snaps[-3:]]
        if recent[2] > recent[1] > recent[0]:
            score += 10

    score = max(0, min(100, score))

    if score >= 70: trend = "accelerating"
    elif score >= 50: trend = "growing"
    elif score >= 30: trend = "slowing"
    else: trend = "declining"

    return score, trend, age_mins


# ── Dead token detection ──────────────────────────────────────────────────────

async def check_dead_resurrection(client, mint, current_mcap):
    if not (settings["mcap_dead_min"] <= current_mcap <= settings["mcap_dead_max"]):
        return False, 0, 0

    try:
        sigs = await rpc(client, "getSignaturesForAddress", [mint, {"limit": 100}])
        if not sigs or not isinstance(sigs, list) or len(sigs) < 10:
            return False, 0, 0

        now = time.time()
        timestamps = sorted([s.get("blockTime", 0) for s in sigs if s.get("blockTime")], reverse=True)

        recent_5min = sum(1 for t in timestamps[:20] if now - t < 300)
        if len(timestamps) > 20:
            gap_hours = (timestamps[0] - timestamps[20]) / 3600
        else:
            gap_hours = 0

        is_resurrection = gap_hours >= 1.5 and recent_5min >= 4
        return is_resurrection, gap_hours, recent_5min
    except Exception:
        return False, 0, 0


# ── Bundle score (inline) ─────────────────────────────────────────────────────

def quick_bundle_score(holders, supply):
    if not holders or not supply:
        return 50
    top10 = sum(h["amount"] for h in holders[:10])
    top10_pct = top10 / supply * 100
    return min(int(top10_pct * 0.7), 100)


# ── Confidence scoring ────────────────────────────────────────────────────────

def calculate_confidence(data):
    score = 0
    reasons = []
    alert_type = data.get("alert_type", "EARLY")

    mcap = data.get("mcap", 0)
    qw = data.get("quality_wallets", [])
    cobuy = data.get("cobuy_count", 0)
    momentum = data.get("momentum_score", 50)
    momentum_trend = data.get("momentum_trend", "")
    organic = data.get("organic_score", 50)
    bundle = data.get("bundle_score", 50)
    age_mins = data.get("age_mins", 0)
    is_resurrection = data.get("is_resurrection", False)
    gap_hours = data.get("gap_hours", 0)
    holder_count = data.get("holder_count", 0)

    # MCap check
    if alert_type == "DEAD":
        if settings["mcap_dead_min"] <= mcap <= settings["mcap_dead_max"]:
            score += 10
    elif alert_type == "CONFIRMED":
        if settings["mcap_confirmed_min"] <= mcap <= settings["mcap_confirmed_max"]:
            score += 10
    else:
        if settings["mcap_min"] <= mcap <= settings["mcap_max"]:
            score += 10

    # Quality wallets
    qw_count = len(qw)
    score += min(qw_count * 8, 24)
    if qw_count >= 2:
        reasons.append(f"{qw_count} quality wallets holding")
    if qw_count >= 5:
        score += 10
        reasons.append("⚡ Coordinated quality entry")

    # Elite wallet check
    elite = [w for w in qw if w["profile"].quality_score >= 85]
    if elite:
        score += 15
        reasons.append(f"🐋 Elite wallet entered ({len(elite)})")

    # Co-buy history
    if cobuy >= 3:
        score += 15
        reasons.append(f"Same wallets co-bought {cobuy}x before")
    elif cobuy >= 1:
        score += 8
        reasons.append(f"Wallets co-bought {cobuy}x before")

    # Momentum
    score += int(momentum * 0.15)
    if momentum >= 70:
        reasons.append(f"Strong momentum ({momentum_trend})")

    # Organic volume
    score += int(organic * 0.1)
    if organic >= 80:
        reasons.append("Organic volume confirmed")
    elif organic < 40:
        score -= 15

    # Bundle
    if bundle <= 20:
        score += 10
        reasons.append("Clean bundle score")
    elif bundle > settings["bundle_max"]:
        score -= 20

    # 3 minute rule
    if age_mins >= 3:
        score += 8
        reasons.append("Survived 3 min rule")

    # Resurrection
    if is_resurrection:
        score += 15
        reasons.append(f"Dead {gap_hours:.1f}h, now resurrecting")

    # Security
    if not data.get("can_mint"):
        score += 3
    if not data.get("can_freeze"):
        score += 3

    score = max(0, min(100, int(score)))

    if score >= 75:
        tier = "🔥 HIGH CONVICTION"
    elif score >= 50:
        tier = "👀 WATCH"
    else:
        tier = "⚠️ RISKY"

    return score, tier, reasons


# ── Alert formatter ───────────────────────────────────────────────────────────

def format_alert(data):
    score, tier, reasons = calculate_confidence(data)
    mint = data["mint"]
    mcap = data.get("mcap", 0)
    price = data.get("price", 0)
    holders = data.get("holder_count", 0)
    qw = data.get("quality_wallets", [])
    cobuy = data.get("cobuy_count", 0)
    organic = data.get("organic_score", 100)
    bundle = data.get("bundle_score", 0)
    momentum_trend = data.get("momentum_trend", "")
    alert_type = data.get("alert_type", "EARLY")
    is_resurrection = data.get("is_resurrection", False)

    type_labels = {
        "EARLY": "🎯 EARLY ENTRY",
        "HIGH_CONVICTION": "🔥 HIGH CONVICTION",
        "CONFIRMED": "💎 CONFIRMED PLAY",
        "DEAD": "💀 DEAD RESURRECTION",
    }
    type_label = type_labels.get(alert_type, "🎯 ALERT")

    lines = [
        f"{type_label}",
        f"",
        f"`{mint}`",
        f"",
        f"*{tier}* `{score}/100`",
        f"",
        f"💰 MCap: `${mcap:,.0f}`",
        f"💵 Price: `${price:.8f}`",
        f"👥 Holders: `{holders}`",
        f"📈 Momentum: `{momentum_trend}`",
        f"",
        f"👛 Quality wallets: `{len(qw)}`",
        f"🤝 Co-buy history: `{cobuy}x`" if cobuy > 0 else f"🤝 Co-buy history: `first time`",
        f"🧺 Organic volume: `{organic}/100`",
        f"🪢 Bundle score: `{bundle}/100`",
        f"🔐 Mint: {'`OPEN ⚠️`' if data.get('can_mint') else '`OK ✅`'} | Freeze: {'`OPEN ⚠️`' if data.get('can_freeze') else '`OK ✅`'}",
        f"",
        f"📋 *Signals:*",
    ]

    for r in reasons[:6]:
        lines.append(f"• {r}")

    lines += [
        f"",
        f"[Dexscreener](https://dexscreener.com/solana/{mint}) | [Photon](https://photon-sol.tinyastro.io/en/lp/{mint})",
    ]

    return "\n".join(lines), score


# ── Token scanner ─────────────────────────────────────────────────────────────

async def evaluate_token(client, mint, app, alert_type_hint=None):
    global paused
    if paused or mint in alerted_tokens:
        return

    snap = await get_token_snapshot(client, mint)
    if not snap:
        return

    mcap = snap.get("mcap", 0)
    if mcap == 0:
        return

    # Security filter
    if snap.get("can_mint") or snap.get("can_freeze"):
        return

    holders = snap.get("holders", [])
    supply = snap.get("supply", 1)
    holder_count = snap.get("holder_count", 0)

    # Determine alert type
    if alert_type_hint == "DEAD":
        is_resurrection, gap_hours, recent_count = await check_dead_resurrection(client, mint, mcap)
        if not is_resurrection:
            return
        alert_type = "DEAD"
    else:
        if settings["mcap_confirmed_min"] <= mcap <= settings["mcap_confirmed_max"]:
            alert_type = "CONFIRMED"
        elif settings["mcap_min"] <= mcap <= settings["mcap_max"]:
            alert_type = "EARLY"
        else:
            return
        is_resurrection = False
        gap_hours = 0

    # Get trades and check organic volume
    trades = await get_recent_trades(client, mint, limit=40)
    organic_score, volume_issues = organic_volume_score(trades, holder_count)
    if organic_score < 35:
        return

    # Buy/sell ratio
    buys = [t for t in trades if t["type"] == "buy"]
    sells = [t for t in trades if t["type"] == "sell"]
    buy_ratio = len(buys) / max(len(trades), 1)
    if buy_ratio < settings["min_buy_sell_ratio"] and alert_type != "DEAD":
        return

    # Unique buyers
    unique_buyers = len(set(t["wallet"] for t in buys))
    if unique_buyers < settings["min_unique_buyers"] and alert_type != "DEAD":
        return

    # Quality wallets
    quality_wallets, avg_quality = await get_quality_wallets(client, holders)
    qw_addrs = [w["wallet"] for w in quality_wallets]

    # Upgrade to HIGH_CONVICTION if quality wallets present at early stage
    if alert_type == "EARLY" and len(quality_wallets) >= 2:
        alert_type = "HIGH_CONVICTION"

    # Co-buy history
    cobuy_count, cobuy_pair = check_cobuy_history(qw_addrs)

    # Momentum
    momentum_score, momentum_trend, age_mins = update_momentum(mint, mcap, holder_count)

    # 3 minute rule for non-dead tokens
    if alert_type in ("EARLY", "HIGH_CONVICTION") and age_mins < 3:
        if mint not in token_history:
            return
        if token_history[mint].get("confirmations", 0) < settings["confirmation_count"]:
            token_history[mint]["confirmations"] = token_history[mint].get("confirmations", 0) + 1
            return

    if momentum_score < 30 and alert_type != "DEAD":
        return

    # Bundle score
    bundle_score = quick_bundle_score(holders, supply)
    if bundle_score > settings["bundle_max"] and alert_type != "DEAD":
        return

    data = {
        **snap,
        "alert_type": alert_type,
        "quality_wallets": quality_wallets,
        "avg_quality": avg_quality,
        "cobuy_count": cobuy_count,
        "momentum_score": momentum_score,
        "momentum_trend": momentum_trend,
        "age_mins": age_mins,
        "organic_score": organic_score,
        "volume_issues": volume_issues,
        "bundle_score": bundle_score,
        "is_resurrection": is_resurrection,
        "gap_hours": gap_hours,
        "buy_ratio": buy_ratio,
        "unique_buyers": unique_buyers,
    }

    score, tier, reasons = calculate_confidence(data)
    if score < 40:
        return

    # Fire alert
    alerted_tokens.add(mint)
    alert_text, conf_score = format_alert(data)

    if ALERT_CHAT_ID and app:
        try:
            await app.bot.send_message(
                chat_id=ALERT_CHAT_ID,
                text=alert_text,
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"send alert: {e}")

    # Update co-buy DB
    if len(qw_addrs) >= 2:
        update_cobuy_db(qw_addrs, mint)

    # Log for performance tracking
    alert_log.append({
        "mint": mint,
        "type": alert_type,
        "tier": tier,
        "score": conf_score,
        "mcap_entry": mcap,
        "mcap_30m": 0,
        "ts": time.time(),
    })

    logger.info(f"Alert: {alert_type} {mint[:8]} score={conf_score} mcap=${mcap:,.0f}")


async def get_new_tokens(client):
    try:
        sigs = await rpc(client, "getSignaturesForAddress", [PUMP_FUN_PROGRAM, {"limit": 15}])
        if not sigs:
            return []
        mints = []
        for sig_info in (sigs or []):
            sig = sig_info.get("signature", "")
            tx = await rpc(client, "getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            if not tx:
                continue
            try:
                for ix in tx["transaction"]["message"]["instructions"]:
                    for acc in ix.get("accounts", []):
                        if isinstance(acc, str) and len(acc) > 40 and acc.endswith("pump"):
                            if acc not in mints and acc not in alerted_tokens:
                                mints.append(acc)
            except Exception:
                continue
        return mints[:5]
    except Exception as e:
        logger.warning(f"get_new_tokens: {e}")
        return []


async def scanner_loop(app):
    logger.info("Scanner started")
    scan_count = 0
    async with httpx.AsyncClient() as client:
        while True:
            try:
                if not paused:
                    # Scan new pump.fun tokens
                    new_mints = await get_new_tokens(client)
                    for mint in new_mints:
                        await evaluate_token(client, mint, app)

                    # Every 10th scan check for dead token resurrections
                    scan_count += 1
                    if scan_count % 10 == 0:
                        for mint in list(alerted_tokens)[-20:]:
                            pass  # Already alerted, skip
                        # Check recent tokens for resurrection
                        if token_history:
                            recent_mints = list(token_history.keys())[-10:]
                            for mint in recent_mints:
                                if mint not in alerted_tokens:
                                    await evaluate_token(client, mint, app, alert_type_hint="DEAD")

                    # Update 30min performance
                    now = time.time()
                    for entry in alert_log:
                        if entry["mcap_30m"] == 0 and now - entry["ts"] >= 1800:
                            snap = await get_token_snapshot(client, entry["mint"])
                            if snap:
                                entry["mcap_30m"] = snap.get("mcap", 0)

            except Exception as e:
                logger.error(f"Scanner error: {e}")

            await asyncio.sleep(12)


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚨 *Alpha Call Bot - Ultimate*\n\n"
        "Monitors Solana 24/7 for high conviction plays.\n\n"
        "*Alert types:*\n"
        "🎯 Early Entry — 5-20k, organic signal\n"
        "🔥 High Conviction — 5-20k + quality wallets\n"
        "💎 Confirmed Play — 20-100k sustained\n"
        "💀 Dead Resurrection — 2-10k revival\n\n"
        "*Filters:*\n"
        "👛 Wallet integrity + farmer detection\n"
        "🧺 Organic volume scoring\n"
        "🤝 Co-buy history network\n"
        "🐋 Elite wallet detection\n"
        "⚡ Coordinated entry detection\n"
        "🪢 Bundle score check\n"
        "⏱ 3 minute survival rule\n"
        "✅ 3x confirmation before alert\n\n"
        "*Commands:*\n"
        "`/setmcap 5000 20000`\n"
        "`/setbundle 35`\n"
        "`/pause` / `/resume`\n"
        "`/topwallets`\n"
        "`/performance`\n"
        "`/stats`",
        parse_mode="Markdown"
    )


async def cmd_setmcap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Usage: `/setmcap 5000 20000`", parse_mode="Markdown")
        return
    try:
        settings["mcap_min"] = int(args[0])
        settings["mcap_max"] = int(args[1])
        await update.message.reply_text(f"MCap range: `${settings['mcap_min']:,}` — `${settings['mcap_max']:,}`", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid values.")


async def cmd_setbundle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/setbundle 35`", parse_mode="Markdown")
        return
    try:
        settings["bundle_max"] = int(args[0])
        await update.message.reply_text(f"Max bundle: `{settings['bundle_max']}`", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid value.")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global paused
    paused = True
    await update.message.reply_text("⏸ Alerts paused.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global paused
    paused = False
    await update.message.reply_text("▶️ Alerts resumed.")


async def cmd_topwallets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not wallet_db:
        await update.message.reply_text("No wallets tracked yet.")
        return
    top = sorted(wallet_db.values(), key=lambda w: w.quality_score, reverse=True)[:10]
    lines = ["👛 *Top Wallets*", ""]
    for i, w in enumerate(top, 1):
        addr = w.address
        farmer = "🚫" if w.is_farmer else "✅"
        lines.append(f"{i}. `{addr[:6]}...{addr[-4:]}` score:`{w.quality_score}` hold:`{w.avg_hold_mins:.0f}m` {farmer}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not alert_log:
        await update.message.reply_text("No alerts fired yet.")
        return

    total = len(alert_log)
    completed = [a for a in alert_log if a["mcap_30m"] > 0]
    wins = [a for a in completed if a["mcap_30m"] > a["mcap_entry"] * 1.2]
    win_rate = len(wins) / max(len(completed), 1) * 100

    by_type = defaultdict(list)
    for a in completed:
        by_type[a["type"]].append(a)

    lines = [
        "📊 *Performance*",
        f"",
        f"Total alerts: `{total}`",
        f"Completed: `{len(completed)}`",
        f"Win rate (>20%): `{win_rate:.1f}%`",
        f"",
        f"*By type:*",
    ]

    for atype, alerts in by_type.items():
        type_wins = sum(1 for a in alerts if a["mcap_30m"] > a["mcap_entry"] * 1.2)
        lines.append(f"{atype}: `{type_wins}/{len(alerts)}` wins")

    lines += ["", "*Recent:*"]
    for a in alert_log[-5:]:
        if a["mcap_30m"] > 0:
            change = (a["mcap_30m"] - a["mcap_entry"]) / a["mcap_entry"] * 100
            emoji = "✅" if change > 20 else "❌"
            lines.append(f"{emoji} `{a['mint'][:8]}` {change:+.0f}% [{a['type']}]")
        else:
            lines.append(f"⏳ `{a['mint'][:8]}` pending [{a['type']}]")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"⚙️ *Settings*\n\n"
        f"Early/High Conv MCap: `${settings['mcap_min']:,}` — `${settings['mcap_max']:,}`\n"
        f"Confirmed MCap: `${settings['mcap_confirmed_min']:,}` — `${settings['mcap_confirmed_max']:,}`\n"
        f"Dead token MCap: `${settings['mcap_dead_min']:,}` — `${settings['mcap_dead_max']:,}`\n"
        f"Max bundle: `{settings['bundle_max']}`\n"
        f"Min quality wallets: `{settings['min_quality_wallets']}`\n"
        f"Min buy ratio: `{settings['min_buy_sell_ratio']:.0%}`\n"
        f"Status: `{'⏸ Paused' if paused else '▶️ Running'}`\n"
        f"Wallets tracked: `{len(wallet_db)}`\n"
        f"Tokens alerted: `{len(alerted_tokens)}`\n"
        f"Alerts fired: `{len(alert_log)}`",
        parse_mode="Markdown"
    )


async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use `/start` to see commands. Alerts fire automatically.", parse_mode="Markdown")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set")
    if not ALERT_CHAT_ID:
        logger.warning("ALERT_CHAT_ID not set — alerts won't be delivered. Add it in Railway variables.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("setmcap", cmd_setmcap))
    app.add_handler(CommandHandler("setbundle", cmd_setbundle))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("topwallets", cmd_topwallets))
    app.add_handler(CommandHandler("performance", cmd_performance))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))

    async def post_init(application):
        asyncio.create_task(scanner_loop(application))

    app.post_init = post_init

    logger.info("Alpha Call Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
