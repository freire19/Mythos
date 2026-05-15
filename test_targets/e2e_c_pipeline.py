"""
End-to-end test: Sprint 1 + 3 integration.
Detect C vulnerability → generate exploit → test in sandbox.
"""
import asyncio, sys
sys.path.insert(0, '.')

async def e2e():
    # Step 1: Detect C vulnerabilities
    from alpha.c_analyzer import CASTAnalyzer
    analyzer = CASTAnalyzer()
    findings = analyzer.scan_file('test_targets/vulnerable/vuln_c_demo.c')
    overflow = [f for f in findings if f['type'] == 'buffer_overflow']
    print(f"Step 1 — Detect: {len(findings)} findings, {len(overflow)} buffer overflows")

    if overflow:
        f = overflow[0]
        print(f"  Selected: {f['function']}() at line {f['line']} — {f['detail']}")

    # Step 2: Check mitigations on a compiled binary
    from alpha.tools.exploit_tools import check_mitigations
    mit = await check_mitigations('test_targets/vulnerable/vuln_bin')
    print(f"Step 2 — Mitigations: {mit.get('mitigations', {})}")

    # Step 3: Generate shellcode
    from alpha.tools.exploit_tools import generate_shellcode, inject_payload
    sc = await generate_shellcode(arch='amd64', shell_type='execve_sh')
    print(f"Step 3 — Shellcode: ok={sc['ok']}")

    # Step 4: Build payload for buffer overflow (offset 72 for vuln_bin)
    pay = await inject_payload(
        target='test_targets/vulnerable/vuln_bin',
        payload_type='buffer_overflow',
        offset=72,
        address='deadbeef',  # Would be actual gadget address
    )
    print(f"Step 4 — Payload: {pay.get('payload_size', 0)} bytes")

    # Step 5: Test in sandbox
    from alpha.sandbox import run_exploit
    if pay.get('ok'):
        result = await run_exploit(
            'test_targets/vulnerable/vuln_bin',
            payload_hex=pay['payload_hex'],
            timeout=5,
        )
        print(f"Step 5 — Sandbox: crashed={result.get('crashed')}, signal={result.get('signal_name', 'none')}")

    print("\n✓ Pipeline C vuln → exploit → sandbox integrado")

asyncio.run(e2e())
