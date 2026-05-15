import sys, tempfile
sys.path.insert(0, '.')

# JS test
from alpha.js_analyzer import scan_js_file
js_code = '''
function unsafe() {
    document.getElementById('x').innerHTML = req.query.name;
    eval('var x = ' + userInput);
    var x = JSON.parse(req.body);
    lodash.merge({}, req.body);
}
'''
f = tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False)
f.write(js_code); f.close()
r = scan_js_file(f.name)
print(f"JS: {len(r)} findings")
for x in r: print(f"  {x['severity']:7s} {x['type']:25s} line {x['line']}")

# Go test
from alpha.go_analyzer import scan_go_file
go_code = '''
package main
import "unsafe"
func main() {
    p := unsafe.Pointer(&x)
    cmd := exec.Command("sh", "-c", userInput)
    var secret = "AKIAIOSFODNN7EXAMPLE"
    go func() { counter += 1 }()
}
'''
f = tempfile.NamedTemporaryFile(mode='w', suffix='.go', delete=False)
f.write(go_code); f.close()
r = scan_go_file(f.name)
print(f"\nGo: {len(r)} findings")
for x in r: print(f"  {x['severity']:7s} {x['type']:25s} line {x['line']}")

# Rust test
from alpha.rust_analyzer import scan_rust_file
rust_code = '''
unsafe fn danger() {
    let p: *const u8 = &x;
    *p;
    std::mem::transmute::<u32, f32>(42);
}
const API_KEY: &str = "sk-1234567890abcdef";
'''
f = tempfile.NamedTemporaryFile(mode='w', suffix='.rs', delete=False)
f.write(rust_code); f.close()
r = scan_rust_file(f.name)
print(f"\nRust: {len(r)} findings")
for x in r: print(f"  {x['severity']:7s} {x['type']:25s} line {x['line']}")

# Reasoning test
from alpha.reasoning import CoTRunner
runner = CoTRunner()
code = '''
void unsafe_copy(char *input) {
    char buf[64];
    strcpy(buf, input);
    printf(input);
}
'''
hypotheses = runner.hypothesize(code, "test.c")
verified = runner.verify(hypotheses, [])
conclusions = runner.conclude(verified)
print(f"\nReasoning: {len(hypotheses)} hypotheses, {len(conclusions)} confirmed")
for h in hypotheses:
    print(f"  [{h.verdict}] {h.statement[:80]}")

# Registry check
from alpha.tools import load_all_tools, TOOL_REGISTRY
load_all_tools()
new = ['analyze_codebase', 'detect_vulns_multi', 'auto_exploit_multi']
cnt = sum(1 for n in new if n in TOOL_REGISTRY)
print(f"\nRegistry: {len(TOOL_REGISTRY)} tools, {cnt}/3 multi-lang")
print("OK")
