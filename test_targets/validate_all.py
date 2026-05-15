"""Full validation of all 4 sprints with real dependencies installed."""
import sys, asyncio
sys.path.insert(0, '.')

async def validate_sprint1():
    print("=== SPRINT 1: Exploit Binário ===")
    from alpha.tools.exploit_tools import check_mitigations, generate_rop_chain, generate_shellcode, inject_payload
    T = 'test_targets/vulnerable/vuln_bin'
    
    r = await check_mitigations(T)
    print(f"  check_mitigations: tool={r['tool']}, nx={r['mitigations']['nx']}, canary={r['mitigations']['stack_canary']}, pie={r['mitigations']['pie']}")

    r = await generate_shellcode(arch='amd64', shell_type='execve_sh')
    print(f"  generate_shellcode: ok={r['ok']}, size={r.get('shellcode_size')}, encoder={r.get('encoder','none')}")

    r = await generate_rop_chain(T, objective='execve_sh')
    print(f"  generate_rop_chain: ok={r['ok']}, arch={r.get('arch','?')}, gadgets={r.get('gadgets_used',0)}")

    r = await inject_payload(target=T, payload_type='buffer_overflow', offset=72, address='401196')
    print(f"  inject_payload: ok={r['ok']}, size={r.get('payload_size',0)}")

async def validate_sprint2():
    print("\n=== SPRINT 2: Embeddings + Graph ===")
    from alpha.tools.code_graph_tools import index_codebase, search_semantic
    from alpha.depgraph import DependencyGraph

    r = await index_codebase('test_targets/aiohttp/aiohttp', force_rebuild=True)
    emb_ok = r.get('embedding_ok', False)
    print(f"  index_codebase: files={r['total_files']}, chunks={r['total_chunks']}, embeddings={emb_ok}")

    if emb_ok:
        r = await search_semantic('test_targets/aiohttp/aiohttp', query='where is user input parsed from HTTP requests?', k=5)
        print(f"  search_semantic: results={r['results_count']}")
        for res in r['results'][:3]:
            print(f"    score={res['score']:.3f} {res['file']}:{res['start_line']} {res['name']}")

    dg = DependencyGraph()
    dg.build('test_targets/aiohttp/aiohttp')
    entries = dg.find_entry_points()
    sinks = dg.find_dangerous_sinks()
    print(f"  depgraph: entry_points={len(entries)}, sinks={len(sinks)}, call_edges={len(dg.call_graph)}")

async def validate_sprint3():
    print("\n=== SPRINT 3: C Analysis ===")
    from alpha.c_analyzer import CASTAnalyzer
    a = CASTAnalyzer()
    r = a.scan_file('test_targets/vulnerable/vuln_c_demo.c')
    print(f"  C scan: findings={len(r)}")
    by_type = {}
    for f in r:
        by_type[f['type']] = by_type.get(f['type'], 0) + 1
    for t, c in sorted(by_type.items()):
        print(f"    {t}: {c}")

async def validate_sprint4():
    print("\n=== SPRINT 4: Feedback Loop ===")
    from alpha.exploit_feedback import ExploitFeedbackLoop
    loop = ExploitFeedbackLoop('test_targets/vulnerable/vuln_bin', bits=64)
    session = await loop.run(max_rounds=8, timeout=5)
    print(f"  success={session.success} offset={session.offset_found} rounds={len(session.rounds)}")
    print(f"  bad_bytes={[f'0x{b:02x}' for b in sorted(session.bad_bytes)]}")
    for r in session.rounds[:5]:
        sig = r.crash.signal if r.crash else '?'
        findings = '; '.join(r.findings[:2]) if r.findings else ''
        print(f"    R{r.round_num}: {r.action:20s} crashed={r.crash.crashed if r.crash else '?'} sig={sig} | {findings[:100]}")

async def main():
    await validate_sprint1()
    await validate_sprint2()
    await validate_sprint3()
    await validate_sprint4()
    print("\n✓ VALIDAÇÃO COMPLETA")

asyncio.run(main())
