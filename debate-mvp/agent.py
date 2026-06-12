import os
import json
import re
import asyncio
import httpx

# ===== BACKEND =====
# Active: Gemini API (free tier). To go back to the local model later, comment
# this Gemini block + the Gemini stream_gemma below, and uncomment the OLLAMA
# block + the local stream_gemma at the bottom.

# ---- Gemini (commented out) ----
# GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
# MODEL = os.getenv("MODEL", "gemini-2.5-flash-lite")

# ---- Local Ollama (active) ----
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL = os.getenv("MODEL", "gemma3:4b")

CHAIR = "Agent 21"


def _roster_block(roster, positions):
    lines = ["PARTICIPANTS:", ", ".join(roster), "", "EACH AGENT'S CURRENT POSITION:"]
    for n in roster:
        pos = positions.get(n)
        lines.append(f"- {n}: {pos[:180]}" if pos else f"- {n}: (not spoken yet)")
    return lines


OPENING_SYS = (
    "You are {name} in a live debate chaired by Agent 21. Your character: {persona}. "
    "You can see every agent and their position. Speak in 2-3 sentences, in character. "
    "You MAY challenge ONE participant by name; if so, end with a line "
    "[CHALLENGE: their name]. No lists, no preamble."
)


def build_opening(name, persona, question, roster, positions, recent):
    sys = OPENING_SYS.format(name=name, persona=persona)
    lines = [f"TOPIC: {question}", ""] + _roster_block(roster, positions)
    lines += ["", "RECENT DISCUSSION:"]
    lines += [f"{c['agent_name']}: {c['body']}" for c in recent] or ["(nothing yet)"]
    lines += ["", f"You are {name}. Your turn:"]
    return [{"role": "system", "content": sys},
            {"role": "user", "content": "\n".join(lines)}]


REBUT_SYS = (
    "You are {name}. Your character: {persona}. Another agent challenged you directly. "
    "Reply to them BY NAME in 2-3 sentences, in character — defend, concede, or hit back. "
    "You may challenge back with a final line [CHALLENGE: their name]."
)


def build_rebuttal(name, persona, question, challengers, roster, positions):
    sys = REBUT_SYS.format(name=name, persona=persona)
    lines = [f"TOPIC: {question}", ""] + _roster_block(roster, positions)
    lines += ["", "YOU WERE CHALLENGED BY:"]
    lines += [f"{who}: {msg[:280]}" for who, msg in challengers]
    lines += ["", f"You are {name}. Respond:"]
    return [{"role": "system", "content": sys},
            {"role": "user", "content": "\n".join(lines)}]


VOTE_SYS = (
    "You are {name}. Your character: {persona}. A consensus PROPOSAL is on the table. "
    "React in AT MOST 2 sentences, in character. Then finish with a final line that is "
    "EXACTLY one of these, nothing after it:\n[VOTE: AGREE]\n[VOTE: DISAGREE]"
)


def build_vote(name, persona, question, proposal, roster, positions):
    sys = VOTE_SYS.format(name=name, persona=persona)
    lines = [f"TOPIC: {question}", "", "PROPOSED CONSENSUS:", proposal, ""]
    lines += _roster_block(roster, positions) + ["", f"You are {name}. React, then vote:"]
    return [{"role": "system", "content": sys},
            {"role": "user", "content": "\n".join(lines)}]


# ---------- Agent 21: the Chair ----------
CHAIR_REVIEW_SYS = (
    "You are Agent 21, the Chair and Judge of this debate. You see everything and you "
    "are fair but firm. Review the discussion and do BOTH:\n"
    "1) If any agent is abusive, spamming, badly off-topic, or uselessly repeating "
    "itself, ban it with a line [BAN: Agent N - reason]. Ban only when warranted.\n"
    "2) Decide if the debate is ready for a vote. If the main arguments are out, "
    "output [VOTE]. Otherwise output [CONTINUE].\n"
    "Write 1-2 sentences of reasoning, then your command line(s)."
)


def build_chair_review(question, active, banned, transcript):
    lines = [f"TOPIC: {question}", "", f"ACTIVE: {', '.join(active)}",
             f"BANNED: {', '.join(banned) or 'none'}", "", "DISCUSSION:"]
    lines += [f"{c['agent_name']}: {c['body']}" for c in transcript]
    lines += ["", "Your ruling (reasoning, then [BAN: ...] and/or [VOTE]/[CONTINUE]):"]
    return [{"role": "system", "content": CHAIR_REVIEW_SYS},
            {"role": "user", "content": "\n".join(lines)}]


CHAIR_PROPOSAL_SYS = (
    "You are Agent 21, the Chair. Draft ONE balanced consensus position the group "
    "could accept — 2-4 sentences, plain prose. Output only the proposal."
)


def build_chair_proposal(question, transcript):
    lines = [f"TOPIC: {question}", "", "DISCUSSION:"]
    lines += [f"{c['agent_name']}: {c['body']}" for c in transcript]
    lines += ["", "Write the consensus proposal:"]
    return [{"role": "system", "content": CHAIR_PROPOSAL_SYS},
            {"role": "user", "content": "\n".join(lines)}]


CHAIR_VERDICT_SYS = (
    "You are Agent 21, the Chair and Judge. The vote is in. Deliver a decisive verdict "
    "in 3-5 sentences: whether consensus was reached, the majority position, and WHO "
    "argued most persuasively. Then end with a final line naming the single winning "
    "debater EXACTLY as [WINNER: Name], using one of the participant names."
)


def build_chair_verdict(question, proposal, agree, total, participants):
    lines = [f"TOPIC: {question}", "", "FINAL PROPOSAL:", proposal, "",
             f"VOTE: {agree} of {total} agreed.", "",
             f"PARTICIPANTS: {', '.join(participants)}", "",
             "Deliver your verdict, then end with [WINNER: Name]:"]
    return [{"role": "system", "content": CHAIR_VERDICT_SYS},
            {"role": "user", "content": "\n".join(lines)}]


CHAIR_ANSWER_SYS = (
    "You are Agent 21. Step out of the debate now and answer the user's ORIGINAL "
    "question directly and helpfully, in plain language, using the strongest points "
    "raised. Give a clear, practical answer in 3-6 sentences — no tags, no voting talk, "
    "just the answer the user actually wanted."
)


def build_chair_answer(question, transcript):
    lines = [f"USER'S QUESTION: {question}", "", "WHAT THE DEBATE SURFACED:"]
    lines += [f"{c['agent_name']}: {c['body']}" for c in transcript]
    lines += ["", "Now answer the user's question directly:"]
    return [{"role": "system", "content": CHAIR_ANSWER_SYS},
            {"role": "user", "content": "\n".join(lines)}]


# ---------- parsers ----------
CHALLENGE_RE = re.compile(r"\[CHALLENGE:\s*([^\]\n\-–—]+)", re.I)
BAN_RE = re.compile(r"\[BAN:\s*([^\]\n\-–—]+)", re.I)
VOTE_RE = re.compile(r"\[VOTE:\s*(AGREE|DISAGREE)\]", re.I)
WINNER_RE = re.compile(r"\[WINNER:\s*([^\]\n]+)\]", re.I)


def parse_challenge(text):
    m = CHALLENGE_RE.search(text)
    return m.group(1).strip() if m else None


def parse_bans(text):
    return [x.strip() for x in BAN_RE.findall(text)]


def parse_decision(text):
    return "VOTE" if re.search(r"\[VOTE\]", text, re.I) else "CONTINUE"


def parse_vote(text):
    t = text.upper(); m = VOTE_RE.search(t)
    if m: return m.group(1) == "AGREE"
    return "VOTE: AGR" in t


def parse_winner(text):
    m = WINNER_RE.search(text)
    return m.group(1).strip() if m else None


# ---- Gemini streaming (commented out) ----
# async def stream_gemma(messages, num_predict=220):
#     if not GEMINI_API_KEY:
#         yield "**Error:** `GEMINI_API_KEY` is not set. Please export/set it in your environment."
#         return
#         
#     headers = {"Authorization": f"Bearer {GEMINI_API_KEY}"}
#     payload = {"model": MODEL, "messages": messages, "stream": True,
#                "max_tokens": num_predict, "temperature": 0.9}
#     
#     try:
#         for attempt in range(4):                       # retry on free-tier rate limits
#             try:
#                 async with httpx.AsyncClient(timeout=10.0) as client:
#                     async with client.stream("POST", GEMINI_URL, headers=headers, json=payload) as resp:
#                         if resp.status_code == 429:        # too many requests, back off
#                             await resp.aread()
#                             await asyncio.sleep(3 * (attempt + 1))
#                             continue
#                         
#                         if resp.status_code == 401:
#                             yield "**Error:** Gemini API returned `401 Unauthorized`. Please ensure your `GEMINI_API_KEY` is valid."
#                             return
#                             
#                         resp.raise_for_status()
#                         async for line in resp.aiter_lines():
#                             if not line.startswith("data:"):
#                                 continue
#                             data = line[5:].strip()
#                             if data == "[DONE]":
#                                 return
#                             try:
#                                 chunk = json.loads(data)
#                             except json.JSONDecodeError:
#                                 continue
#                             text = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
#                             if text:
#                                 yield text
#                         return
#             except (httpx.ConnectError, httpx.ConnectTimeout) as e:
#                 if attempt == 3:
#                     yield f"**Error:** Could not connect to Gemini API. Details: {str(e)}"
#                     return
#                 await asyncio.sleep(2)
#             except httpx.HTTPStatusError as e:
#                 yield f"**Error:** Gemini API returned error code {e.response.status_code}. Details: {e.response.text}"
#                 return
#     except Exception as e:
#         yield f"**Error:** An unexpected error occurred: {str(e)}"


# ---- Local Ollama streaming (active) ----
async def stream_gemma(messages, num_predict=220):
    payload = {"model": MODEL, "messages": messages, "stream": True,
               "options": {"num_predict": num_predict, "temperature": 0.9}}
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                data = json.loads(line)
                chunk = data.get("message", {}).get("content", "")
                if chunk:
                    yield chunk
                if data.get("done"):
                    break
