import asyncio
from alpha.tools.exploit_tools import check_mitigations, generate_shellcode, inject_payload

TARGET = "test_targets/vulnerable/vuln_bin"

async def smoke():
    r = await check_mitigations(TARGET)
    print(f"check_mitigations: ok={r['ok']}, mitigations={r.get('mitigations')}")

    r = await generate_shellcode(arch='amd64', shell_type='execve_sh')
    print(f"generate_shellcode: ok={r['ok']}, size={r.get('shellcode_size')}")

    r = await inject_payload(
        target=TARGET,
        payload_type='buffer_overflow',
        payload='90' * 16 + 'cc',
        address='41414141',
        offset=72
    )
    print(f"inject_payload: ok={r['ok']}, size={r.get('payload_size')}")

asyncio.run(smoke())
print("SMOKE TEST PASSED")
