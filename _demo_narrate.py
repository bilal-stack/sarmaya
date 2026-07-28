"""Generate AI narration clips for the demo video (free, local, no API key).

Uses edge-tts (Microsoft neural voices). Writes ONE MP3 PER SCENE into
demo_assets/narration/ plus a timing sheet, so you can drop each clip on the
timeline and cut the screen recording to fit it.

Usage:
    .\.venv\Scripts\python.exe _demo_narrate.py                 # default voice
    .\.venv\Scripts\python.exe _demo_narrate.py --voice en-GB-RyanNeural
    .\.venv\Scripts\python.exe _demo_narrate.py --list-voices   # browse voices
    .\.venv\Scripts\python.exe _demo_narrate.py --rate -8%      # slow it down

The text below is TTS-tuned: numbers spelled out, no markdown, short sentences,
and paragraph breaks that the engine renders as natural pauses.
"""
import argparse
import asyncio
import os
import re

import edge_tts

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_assets", "narration")

# Good picks: en-US-AndrewNeural (warm, natural), en-US-BrianNeural (crisp),
# en-GB-RyanNeural (British), en-US-AriaNeural / en-US-JennyNeural (female).
DEFAULT_VOICE = "en-US-AndrewNeural"
DEFAULT_RATE = "-5%"   # slightly slower than default reads better over UI demos

SCENES = {
    "00_hook": """
This is Sarmaya O S, an accounts payable automation backend I built with Fast A P I, Postgres, and Claude.

Most invoice tools stop at data capture. This one is built around governance. Every policy is configuration. Every decision is explainable. And every action, including every A I action, is recorded in a tamper evident audit trail.

Let me show you what that means in practice.
""",

    "01_extraction": """
I'll upload a real invoice P D F.

Behind the scenes, two things happen. First, O C R pulls the raw text. Then Claude cleans it up. It merges fragmented line item descriptions and normalizes the fields into structured J SON.

And that's the important part. The A I's output isn't trusted blindly. It's validated against a strict schema before it touches the invoice. If the model returns something malformed, the result is rejected, and the raw O C R data stands. The A I assists. It never decides.

We get the vendor, invoice number, date, total, and tax, plus a confidence score that drives what happens next.
""",

    "01b_providers": """
And none of this is tied to one vendor. The O C R engine and the language model both sit behind a common provider interface, so the system is built to run on the mainstream O C R and A I providers. Swapping one for another is a configuration change, not a rewrite.

That matters when a client already has cloud agreements in place, or data residency rules about where their documents can be processed.
""",

    "02_duplicate": """
Now I'll upload a second invoice from the same vendor. Different invoice number, but the amount is within a third of a percent, and it's dated three days later.

The system flags it as a potential duplicate. Not a hard block, a soft warning, because sometimes those are legitimate. But it cannot be approved until a human reviews it and overrides it with a written reason. And that reason goes into the audit trail.
""",

    "03_inbox": """
This is the Decision Inbox. One prioritized worklist across everything waiting on you.

Each pending invoice is reduced to its single most blocking next step. Not a list of invoices, a list of decisions.

This one is blocked because its vendor isn't verified yet. These are waiting on approval. And notice the first one is flagged overdue. It's been sitting in pending approval for seventy two hours, past the forty eight hour S L A. Breached items sort to the top automatically.

And I can filter to just the S L A breaches.
""",

    "04_escalation": """
S L As are configured per workflow state. Forty eight hours in pending approval, then escalate to the C F O. The timer starts the moment an invoice enters a state.

Running the escalation records an audit event and notifies the C F O. And it's idempotent. It escalates each breach exactly once per state entry, so I can safely wire it to a cron job.

The escalated invoice also becomes visible in the C F O's inbox, even though the original approver was a manager. The original approval chain is preserved in the trail. Escalation adds to it. It doesn't rewrite it.
""",

    "05_agent": """
This is the workflow agent. You ask it, what should happen to this invoice next?

It answers, verify vendor. And it shows its work. These are the signals it reasoned from. The invoice is pending approval, and the vendor is still pending verification.

With A I enabled, Claude writes the explanation and scores its confidence. But here's the critical design decision. The A I is not allowed to choose the action. Policy determines what's permitted. The model can only phrase it.

If the model tries to return a different action, say it decides to just approve something, that output is discarded, the deterministic result stands, and the attempt is logged as a schema failure. That's tested.
""",

    "06_governance": """
Now the controls. Segregation of duties. This manager created this invoice, so they cannot approve it. Maker checker, enforced in the service layer. And the blocked attempt is itself written to the audit trail.

And here's the vendor gate. This invoice's vendor is still pending verification, so no one can approve it. Not even an admin. Money doesn't move against an unverified vendor. Someone with vendor management rights has to verify that vendor first, and they can't be the person who created it either.
""",

    "07_audit": """
Every object opens as a full timeline. What happened, when, who did it, and why. Each event carries a plain English reason, including the exact policy that routed the approval, snapshotted at the moment of the decision. So if the policy changes later, the history still shows what rule actually applied.

And the audit trail is tamper evident. Every entry is hash chained to the one before it. If someone edited or deleted a row directly in the database, this check would fail, and tell you exactly which event broke.

The same discipline applies to A I. Every single A I call is logged. Which model, which prompt version, the confidence, the latency, and whether the output passed schema validation or fell back. Full reproducibility.
""",

    "08_close": """
One last thing. This is a natural language query agent. You ask a question in plain English, and it turns that into a database query using tool calling, then answers from the real data.

It's scoped to your tenant, enforced both in the tool itself and by row level security in Postgres, so it cannot reach another client's data even if the model tried. And like every other A I call in the system, the question, the model, and the result are written to the A I action log. Convenience for the user, without opening a hole in the governance.

So, everything you saw is configuration, not hardcode. Approval thresholds, workflow states, transition guards, S L As. All editable through the A P I, all versioned, and any version can be rolled back.

The whole thing is multi tenant with Postgres row level security, and it's covered by two hundred and sixty one tests.

Thanks for watching.
""",
}


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", text))


async def synth(name: str, text: str, voice: str, rate: str) -> None:
    path = os.path.join(OUT, f"{name}.mp3")
    await edge_tts.Communicate(text.strip(), voice, rate=rate).save(path)
    # ~150 wpm at normal rate; the rate flag shifts it a little.
    est = word_count(text) / 150 * 60
    print(f"  {name+'.mp3':<24} ~{est:4.0f}s  ({word_count(text)} words)")


async def main(voice: str, rate: str) -> None:
    os.makedirs(OUT, exist_ok=True)
    print(f"\nVoice: {voice}   Rate: {rate}\nWriting to {OUT}\n")
    total = 0
    for name, text in SCENES.items():
        await synth(name, text, voice, rate)
        total += word_count(text) / 150 * 60
    print(f"\n  {'TOTAL':<24} ~{total:4.0f}s  ({total/60:.1f} min of narration)")
    print("\nNext: drop these on your timeline in order, then cut the screen")
    print("recording to fit each clip. See docs/DEMO_AI_NARRATION.md\n")


async def list_voices() -> None:
    voices = await edge_tts.list_voices()
    for v in sorted(voices, key=lambda x: x["ShortName"]):
        if v["Locale"].startswith("en-"):
            print(f"{v['ShortName']:<28} {v['Gender']:<7} {v['Locale']}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--voice", default=DEFAULT_VOICE)
    p.add_argument("--rate", default=DEFAULT_RATE, help="e.g. -10%%, +5%%")
    p.add_argument("--list-voices", action="store_true")
    a = p.parse_args()
    asyncio.run(list_voices() if a.list_voices else main(a.voice, a.rate))
