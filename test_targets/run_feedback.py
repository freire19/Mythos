import sys,asyncio
sys.path.insert(0,'.')
from alpha.exploit_feedback import ExploitFeedbackLoop

async def main():
    loop = ExploitFeedbackLoop('test_targets/vulnerable/vuln_bin', bits=64)
    session = await loop.run(max_rounds=6, timeout=5)
    print(f"Success: {session.success}")
    print(f"Offset found: {session.offset_found}")
    print(f"Bad bytes: {[f'0x{b:02x}' for b in sorted(session.bad_bytes)]}")
    print(f"Rounds: {len(session.rounds)}")
    for r in session.rounds:
        sig = r.crash.signal if r.crash else '?'
        print(f"  R{r.round_num}: {r.action:20s} off={r.offset:3d} crashed={r.crash.crashed if r.crash else '?'} sig={sig}")
        for f in r.findings:
            print(f"    -> {f}")

asyncio.run(main())
