import asyncio, sys
sys.path.insert(0, '.')
from alpha.tools.code_graph_tools import index_codebase, trace_dataflow

async def compare():
    r = await index_codebase('test_targets/aiohttp/aiohttp', force_rebuild=True)
    print(f"Index: {r['total_files']} files, {r['total_chunks']} chunks")
    print(f"Entry points: {len(r['entry_points'])}")
    print(f"Top security chunks: {len(r['top_security_chunks'])}")

    r2 = await trace_dataflow('test_targets/aiohttp/aiohttp')
    print(f"Sinks: {len(r2.get('dangerous_sinks',[]))}, Paths: {r2.get('paths_found',0)}")

asyncio.run(compare())
print("OK")
