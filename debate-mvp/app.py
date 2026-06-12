import json
import random
import asyncio
from pathlib import Path
import httpx

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

import db
from agent import (
    CHAIR,
    MODEL,
    build_opening,
    build_rebuttal,
    build_vote,
    build_chair_review,
    build_chair_proposal,
    build_chair_verdict,
    build_chair_answer,
    stream_gemma,
    parse_challenge,
    parse_bans,
    parse_decision,
    parse_vote,
    parse_winner,
)
from personas import PERSONAS

# OLLAMA_URL and GEMINI_API_KEY might be dynamically commented out in agent.py
try:
    from agent import OLLAMA_URL
except ImportError:
    OLLAMA_URL = None

try:
    from agent import GEMINI_API_KEY
except ImportError:
    GEMINI_API_KEY = None

app = FastAPI(title="Consensus Debate with Roster Resolver Server")

# Allow CORS for ease of development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

db.init_db()

STATIC_DIR = Path(__file__).parent / "static"

CONCURRENCY = 2
MAX_DEBATE_ROUNDS = 3      # cap; Agent 21 can call the vote sooner
THRESHOLD = 0.6
WINDOW = 12
CHAIR_WINDOW = 50          # how much of the transcript Agent 21 reviews at once


def resolve(raw, roster):
    if not raw:
        return None
    s = raw.strip().lower()
    for n in roster:                       # exact name first (handles Lena vs Elena)
        if n.lower() == s:
            return n
    for n in roster:                       # then loose match
        if n.lower() in s or s in n.lower():
            return n
    return None


@app.get("/")
async def index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("Frontend file index.html not found.", status_code=404)
    return HTMLResponse(index_file.read_text())


@app.get("/api/posts")
async def get_posts():
    return db.get_posts()


@app.get("/api/posts/{post_id}")
async def get_post_detail(post_id: int):
    post = db.get_post(post_id)
    if not post:
        return {"error": "Post not found"}, 404
    comments = db.get_comments(post_id)
    return {"post": post, "comments": comments}


@app.get("/api/status")
async def get_status():
    if GEMINI_API_KEY is not None:
        has_key = bool(GEMINI_API_KEY)
        return {
            "backend": "gemini",
            "connected": has_key,
            "model_configured": MODEL,
            "model_available": has_key,
            "details": "Using Gemini API" if has_key else "Missing GEMINI_API_KEY in environment"
        }
    
    if OLLAMA_URL:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{OLLAMA_URL}/api/tags")
                if resp.status_code == 200:
                    data = resp.json()
                    models = [m["name"] for m in data.get("models", [])]
                    model_available = False
                    for m in models:
                        if m == MODEL or m.startswith(MODEL + ":") or MODEL.startswith(m + ":"):
                            model_available = True
                            break
                    return {
                        "backend": "ollama",
                        "connected": True,
                        "model_configured": MODEL,
                        "model_available": model_available,
                        "details": f"Ollama running, model active: {model_available}"
                    }
        except Exception:
            pass
    return {
        "backend": "ollama",
        "connected": False,
        "model_configured": MODEL,
        "model_available": False,
        "details": "Ollama offline"
    }


async def stream_one(ws, lock, name, messages, results, num_predict=220):
    full = ""
    try:
        async for token in stream_gemma(messages, num_predict=num_predict):
            full += token
            async with lock:
                await ws.send_json({"type": "token", "agent": name, "text": token})
    except Exception as e:
        full = f"**Error:** {str(e)}"
    
    results[name] = full
    async with lock:
        await ws.send_json({"type": "turn_done", "agent": name})


async def run_batch(ws, lock, items):
    results = {}
    await ws.send_json({"type": "speakers", "agents": [n for n, _ in items]})
    await asyncio.gather(*[stream_one(ws, lock, n, m, results) for n, m in items])
    return results


def save(post_id, name, body):
    db.add_comment(post_id, name, body,
                   parent_id=db.last_comment_id(post_id),
                   turn=len(db.get_comments(post_id)))


def positions_of(post_id, roster):
    pos = {}
    for c in db.get_comments(post_id):
        if c["agent_name"] in roster:
            pos[c["agent_name"]] = c["body"]   # keep latest
    return pos


async def chair_say(ws, lock, post_id, messages, num_predict=260):
    full = ""
    await ws.send_json({"type": "speakers", "agents": [CHAIR]})
    try:
        async for token in stream_gemma(messages, num_predict=num_predict):
            full += token
            async with lock:
                await ws.send_json({"type": "token", "agent": CHAIR, "text": token})
    except Exception as e:
        full = f"**Error:** {str(e)}"
        async with lock:
            await ws.send_json({"type": "token", "agent": CHAIR, "text": full})
    
    async with lock:
        await ws.send_json({"type": "turn_done", "agent": CHAIR})
    return full


async def chair_text(messages, num_predict=300):
    full = ""
    async for tok in stream_gemma(messages, num_predict=num_predict):
        full += tok
    return full


@app.websocket("/ws")
async def ws(ws: WebSocket):
    await ws.accept()
    lock = asyncio.Lock()
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            question = (msg.get("question") or "").strip()
            post_id = msg.get("post_id")
            if not question:
                continue

            if post_id is None:
                post_id = db.create_post(question)
                await ws.send_json({"type": "post", "post_id": post_id})
            else:
                save(post_id, "User", question)

            q = db.get_post(post_id)["question"]
            active = [n for n, _ in PERSONAS]
            persona_of = dict(PERSONAS)
            banned = []

            # ---------- debate rounds ----------
            decision = "CONTINUE"
            has_error = False
            for rnd in range(1, MAX_DEBATE_ROUNDS + 1):
                await ws.send_json({"type": "phase", "label": f"Debate round {rnd}"})
                order = active[:]; random.shuffle(order)
                challenges = {}
                for i in range(0, len(order), CONCURRENCY):
                    batch = order[i:i + CONCURRENCY]
                    positions = positions_of(post_id, active)
                    recent = db.get_comments(post_id)   # full discussion — everyone reads every comment
                    items = [(n, build_opening(n, persona_of[n], q, active, positions, recent)) for n in batch]
                    res = await run_batch(ws, lock, items)
                    for n in batch:
                        val = res.get(n, "")
                        save(post_id, n, val)
                        if val.startswith("**Error:**"):
                            has_error = True
                        tgt = resolve(parse_challenge(val), active)
                        if tgt and tgt != n:
                            challenges.setdefault(tgt, []).append((n, val))
                            await ws.send_json({"type": "challenge", "from": n, "to": tgt})

                if has_error:
                    break

                # ---------- rebuttals ----------
                targets = [t for t in active if t in challenges]
                if targets:
                    await ws.send_json({"type": "phase", "label": "Rebuttals"})
                    random.shuffle(targets)
                    for i in range(0, len(targets), CONCURRENCY):
                        batch = targets[i:i + CONCURRENCY]
                        positions = positions_of(post_id, active)
                        items = [(n, build_rebuttal(n, persona_of[n], q, challenges[n], active, positions)) for n in batch]
                        res = await run_batch(ws, lock, items)
                        for n in batch:
                            val = res.get(n, "")
                            save(post_id, n, val)
                            if val.startswith("**Error:**"):
                                has_error = True
                
                if has_error:
                    break

                # ---------- Agent 21 reviews: ban + decide ----------
                await ws.send_json({"type": "phase", "label": "Agent 21 reviewing"})
                review = await chair_say(ws, lock, post_id,
                                         build_chair_review(q, active, banned,
                                                            db.get_comments(post_id)[-CHAIR_WINDOW:]))
                save(post_id, CHAIR, review)
                if review.startswith("**Error:**"):
                    has_error = True
                    break
                for raw in parse_bans(review):
                    b = resolve(raw, active)
                    if b and b in active:
                        active.remove(b); banned.append(b)
                        await ws.send_json({"type": "ban", "agent": b})
                decision = parse_decision(review)
                if decision == "VOTE":
                    break

            if has_error:
                db.update_post_status(post_id, "error")
                await ws.send_json({"type": "final", "text": "Debate aborted due to connection error.", "consensus": False})
                continue

            # ---------- vote ----------
            await ws.send_json({"type": "phase", "label": "Agent 21 calls a vote"})
            proposal = await chair_say(ws, lock, post_id,
                                       build_chair_proposal(q, db.get_comments(post_id)[-CHAIR_WINDOW:]))
            save(post_id, CHAIR, proposal)
            if proposal.startswith("**Error:**"):
                db.update_post_status(post_id, "error")
                await ws.send_json({"type": "final", "text": "Debate aborted due to connection error.", "consensus": False})
                continue
            await ws.send_json({"type": "proposal", "text": proposal})

            await ws.send_json({"type": "phase", "label": "Voting"})
            order = active[:]; random.shuffle(order)
            votes = {}
            for i in range(0, len(order), CONCURRENCY):
                batch = order[i:i + CONCURRENCY]
                positions = positions_of(post_id, active)
                items = [(n, build_vote(n, persona_of[n], q, proposal, active, positions)) for n in batch]
                res = await run_batch(ws, lock, items)
                for n in batch:
                    val = res.get(n, "")
                    votes[n] = parse_vote(val); save(post_id, n, val)
                    if val.startswith("**Error:**"):
                        has_error = True
            
            if has_error:
                db.update_post_status(post_id, "error")
                await ws.send_json({"type": "final", "text": "Debate aborted due to connection error.", "consensus": False})
                continue

            agree, total = sum(votes.values()), max(len(votes), 1)
            await ws.send_json({"type": "tally", "agree": agree, "total": total})

            # ---------- verdict ----------
            await ws.send_json({"type": "phase", "label": "Agent 21 delivers the verdict"})
            verdict = await chair_say(ws, lock, post_id,
                                      build_chair_verdict(q, proposal, agree, total, active))
            save(post_id, CHAIR, verdict)
            if verdict.startswith("**Error:**"):
                db.update_post_status(post_id, "error")
                await ws.send_json({"type": "final", "text": "Debate aborted due to connection error.", "consensus": False})
                continue
            
            winner = resolve(parse_winner(verdict), active)   # the agent Agent 21 judged best
            is_consensus = agree / total >= THRESHOLD
            db.update_post_status(post_id, "consensus" if is_consensus else "best_effort")
            await ws.send_json({
                "type": "final",
                "text": verdict,
                "consensus": is_consensus,
                "winner": winner,        # may be None if it didn't tag one
                "banned": banned,        # list of agents ejected this debate (often empty)
            })
            
            # ---------- final result ----------
            answer = await chair_text(build_chair_answer(q, db.get_comments(post_id)[-CHAIR_WINDOW:]))
            save(post_id, CHAIR, answer)
            await ws.send_json({"type": "result", "text": answer})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
