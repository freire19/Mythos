# Changelog

## [1.22.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.21.10...alpha-code-v1.22.0) (2026-05-20)


### Features

* ※ recap line for end-of-task summaries ([6520ffc](https://github.com/freire19/Alpha_Code/commit/6520ffc6562c5f8ac650aa21ca6a6dc5cbe22822))

## [1.21.10](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.21.9...alpha-code-v1.21.10) (2026-05-20)


### Refactoring

* /simplify pass on session 2026-05-20 commits (round 2) ([ac849b6](https://github.com/freire19/Alpha_Code/commit/ac849b60b35f9b6f34d6c5a3e37999036044855a))

## [1.21.9](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.21.8...alpha-code-v1.21.9) (2026-05-20)


### Refactoring

* **browser:** extract tab management to _browser_tabs.py (closes #DM029) ([8343714](https://github.com/freire19/Alpha_Code/commit/834371442639390786e2c792f1b916506c536bb1))
* extract _run_subagent helpers (closes #DM040 partial) ([165dad4](https://github.com/freire19/Alpha_Code/commit/165dad4fbda53f8675496e781fb15f24ccda1b9f))
* extract run_repl helpers in main.py (closes #DM020) ([32c1734](https://github.com/freire19/Alpha_Code/commit/32c173413856a7e167709e12c1a4051f14fcb20f))
* extract stream_anthropic helpers (closes #DM040 partial) ([ae65512](https://github.com/freire19/Alpha_Code/commit/ae655127281f987d3517b79dabd494b01752ec68))

## [1.21.8](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.21.7...alpha-code-v1.21.8) (2026-05-20)


### Bug Fixes

* **repl:** bind Ctrl+C to clear input instead of exiting ([341970f](https://github.com/freire19/Alpha_Code/commit/341970f4a7f349759ea3ded48bebb28aebba7751))


### Refactoring

* **cli:** split commands.py into sub-package by domain (closes #DM041) ([3b0f0ba](https://github.com/freire19/Alpha_Code/commit/3b0f0ba85525bfe0962bce08dc1646222b1b016c))
* extract run_agent helpers (closes #DM038) ([682a8a7](https://github.com/freire19/Alpha_Code/commit/682a8a721120ace7ff3746152225c31d5e230ef3))
* extract stream_chat_with_tools helpers (closes #DM039) ([1f8ae35](https://github.com/freire19/Alpha_Code/commit/1f8ae3524a4c2cfbad04c8cdbab6e200c80a64c0))

## [1.21.7](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.21.6...alpha-code-v1.21.7) (2026-05-20)


### Refactoring

* /simplify pass on session 2026-05-20 commits ([31f03f0](https://github.com/freire19/Alpha_Code/commit/31f03f0777076b1828eb82dc66386026b48d5ee5))

## [1.21.6](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.21.5...alpha-code-v1.21.6) (2026-05-20)


### Performance

* lazy %s logger formatting in hot paths (closes #DM049) ([3f334c8](https://github.com/freire19/Alpha_Code/commit/3f334c82fd5670c398db9e653d6e87ddac203813))


### Refactoring

* triage broad except: pass — log instead of silent (closes #DM043) ([73da98f](https://github.com/freire19/Alpha_Code/commit/73da98fe9022ddd4c5421d69acf12acae7b20d71))


### Documentation

* expand module headers in large files (closes #DM047) ([5fd39cf](https://github.com/freire19/Alpha_Code/commit/5fd39cf6fbe303a560398f58036bdd7fd4c0e366))

## [1.21.5](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.21.4...alpha-code-v1.21.5) (2026-05-20)


### Bug Fixes

* close DEEP_RESILIENCE V4.3 medium/low cluster (10 issues) ([2c814e4](https://github.com/freire19/Alpha_Code/commit/2c814e4d5016966a480e6666b21c982a71f15fbd))

## [1.21.4](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.21.3...alpha-code-v1.21.4) (2026-05-20)


### Bug Fixes

* **display:** _print_result_body re-export + e2e test for delegation ([172ddc0](https://github.com/freire19/Alpha_Code/commit/172ddc0850bf002c7bd118b07c280cea8e257653))

## [1.21.3](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.21.2...alpha-code-v1.21.3) (2026-05-20)


### Performance

* close DEEP_PERFORMANCE V5.1 (P001-P004) + simplify cleanup ([6792987](https://github.com/freire19/Alpha_Code/commit/679298773b341c5c2561e87ad9cc6fb66bd5e57f))

## [1.21.2](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.21.1...alpha-code-v1.21.2) (2026-05-20)


### Bug Fixes

* lifecycle hooks, price_for boundary, cluster_answers, _format_tool_call_header import ([da438c4](https://github.com/freire19/Alpha_Code/commit/da438c458a3520a1bb05eaf0bfdbec4318059e5c))
* **security:** close DEEP_SECURITY V3.3 — 13 findings (1 CRIT, 2 ALTO, 4 MED, 6 BAIXO) ([6b04259](https://github.com/freire19/Alpha_Code/commit/6b0425928e3da07919e1d4748985245ed2a2882a))


### Refactoring

* extract LoopAwareClient httpx singleton (closes #DM042, #DM032) ([56c15b3](https://github.com/freire19/Alpha_Code/commit/56c15b3c1baa4039ab27712b7504918f6debad28))
* **git:** dict dispatch for _git_operation (closes #DM044) ([f6a1d47](https://github.com/freire19/Alpha_Code/commit/f6a1d47f84467f813db18c8790c6c7f1efb54b6a))
* **tools:** extract _resolve_target helper for composites (closes #DM025) ([e982447](https://github.com/freire19/Alpha_Code/commit/e9824471e8d65ce1a048d93caaad885e367ac277))


### Documentation

* document missing env vars in .env.example (closes #DM045) ([31afcb4](https://github.com/freire19/Alpha_Code/commit/31afcb40df047b45c48f0e039fad3c1842ac3bee))

## [1.21.1](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.21.0...alpha-code-v1.21.1) (2026-05-19)


### Refactoring

* /simplify pass on pre-flight feature (slices 1+2+2.5) ([6054e04](https://github.com/freire19/Alpha_Code/commit/6054e0452ae22bd539166a9c44c1c3642edc3df2))

## [1.21.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.20.0...alpha-code-v1.21.0) (2026-05-19)


### Features

* /preflight REPL command — analytics on the feedback log ([658afdc](https://github.com/freire19/Alpha_Code/commit/658afdcc8edad690c10443907fcca99a516074ab))

## [1.20.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.19.0...alpha-code-v1.20.0) (2026-05-19)


### Features

* pre_flight slice 2 — enforcement + session cap + feedback log ([cf0c034](https://github.com/freire19/Alpha_Code/commit/cf0c034b6ddc380d7425b2d40b9a1a53c01f7456))

## [1.19.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.18.1...alpha-code-v1.19.0) (2026-05-19)


### Features

* pre_flight cards — strategy approval before tool batches (slice 1) ([1ce5697](https://github.com/freire19/Alpha_Code/commit/1ce5697f5490a702f6709e0aa43e7141012c10e4))


### Documentation

* RFC for pre-flight cards — strategy approval before execution ([0992056](https://github.com/freire19/Alpha_Code/commit/099205685ceb7a94dc1bb9844d01c55384dff117))

## [1.18.1](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.18.0...alpha-code-v1.18.1) (2026-05-19)


### Refactoring

* /simplify pass on bundled-agents commit ([14828b6](https://github.com/freire19/Alpha_Code/commit/14828b6b0bc49a6e22af47675582e88bd7f911e6))

## [1.18.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.17.0...alpha-code-v1.18.0) (2026-05-19)


### Features

* bundle built-in agents inside the wheel — pipx UX gap ([813309e](https://github.com/freire19/Alpha_Code/commit/813309e94b557119fa6522f9a52c7fa2bdaf909a))

## [1.17.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.16.0...alpha-code-v1.17.0) (2026-05-19)


### Features

* standalone binary via PyInstaller — Plano-v3 §4 Tier 2 ([a95a867](https://github.com/freire19/Alpha_Code/commit/a95a867dbcd9a29011fa178e33e4ac367a6ac118))

## [1.16.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.15.0...alpha-code-v1.16.0) (2026-05-19)


### Features

* PyPI Tier 1 — dotenv discovery + project metadata — Plano-v3 §4 ([c584013](https://github.com/freire19/Alpha_Code/commit/c5840133dbc597a48224b22b8dbd9df6bd9eba20))
* wire firejail/bwrap sandbox into execute_python — Plano-v3 §1.3 ([bf5d5b8](https://github.com/freire19/Alpha_Code/commit/bf5d5b88a37291e960ce790697a73e3ff9370994))


### Refactoring

* centralize tolerant JSON file reads — Plano-v3 §2.3 ([1268f20](https://github.com/freire19/Alpha_Code/commit/1268f20c33eb12d52a3b6f9efe9217f58ea2a6c5))

## [1.15.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.14.0...alpha-code-v1.15.0) (2026-05-18)


### Features

* /pdf and /audio attachments via text extraction — H3 [#18](https://github.com/freire19/Alpha_Code/issues/18) ([e2a1697](https://github.com/freire19/Alpha_Code/commit/e2a1697c8ce8c396986be31bfbdc0030add6ba8d))

## [1.14.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.13.0...alpha-code-v1.14.0) (2026-05-18)


### Features

* optional firejail/bubblewrap sandbox for destructive shell tools — H3 [#14](https://github.com/freire19/Alpha_Code/issues/14) ([6920d42](https://github.com/freire19/Alpha_Code/commit/6920d42b6781496f03636580ad8f301a59627442))

## [1.13.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.12.0...alpha-code-v1.13.0) (2026-05-18)


### Features

* skill registry + alpha skills install/list/remove/update — H3 [#16](https://github.com/freire19/Alpha_Code/issues/16) ([0d179af](https://github.com/freire19/Alpha_Code/commit/0d179af90c6dbf58382dd4ac76230e69fefc835c))

## [1.12.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.11.2...alpha-code-v1.12.0) (2026-05-18)


### Features

* Docker image with two-stage build — H3 [#15](https://github.com/freire19/Alpha_Code/issues/15) ([937a3df](https://github.com/freire19/Alpha_Code/commit/937a3dfa9b27310d5073bd873f99f0a3647018cd))

## [1.11.2](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.11.1...alpha-code-v1.11.2) (2026-05-18)


### Refactoring

* bundle prompts inside alpha package — H3 [#13](https://github.com/freire19/Alpha_Code/issues/13) phase 1 ([28a924b](https://github.com/freire19/Alpha_Code/commit/28a924bf148fd6e2503686270756ee629b3ba287))

## [1.11.1](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.11.0...alpha-code-v1.11.1) (2026-05-18)


### Refactoring

* /simplify pass on H2 — cleanup before H3 ([502ac7c](https://github.com/freire19/Alpha_Code/commit/502ac7ca0840f241786c6a29c3da7f91448eb2af))

## [1.11.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.10.0...alpha-code-v1.11.0) (2026-05-18)


### Features

* cross-session memory — H2 [#10](https://github.com/freire19/Alpha_Code/issues/10) phase 1 ([34c5bbd](https://github.com/freire19/Alpha_Code/commit/34c5bbd93777ab0c2ee116aead65eba49843fce6))
* session replay CLI (replay phase 2) — H2 [#9](https://github.com/freire19/Alpha_Code/issues/9) ([423f8b8](https://github.com/freire19/Alpha_Code/commit/423f8b852b890e91117e3e9b0575b16281a079de))

## [1.10.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.9.0...alpha-code-v1.10.0) (2026-05-18)


### Features

* delegate_consensus + observability features in README — H2 [#8](https://github.com/freire19/Alpha_Code/issues/8) ([d7b97a2](https://github.com/freire19/Alpha_Code/commit/d7b97a2d43bf5430a51d3e73104e4fe2dbe8bc93))
* session recording (replay phase 1) — H2 [#9](https://github.com/freire19/Alpha_Code/issues/9) ([08e0d55](https://github.com/freire19/Alpha_Code/commit/08e0d5536a66813d8e2c222f240fad57b78d7f7f))


### Refactoring

* ProviderProtocol + registry — H2 [#7](https://github.com/freire19/Alpha_Code/issues/7) ([00376fc](https://github.com/freire19/Alpha_Code/commit/00376fc5eb40466f8df1d96acf3f1462185ff33e))

## [1.9.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.8.7...alpha-code-v1.9.0) (2026-05-18)


### Features

* /stats command + opt-in JSON logs — H1 [#4](https://github.com/freire19/Alpha_Code/issues/4) phases 2 & 3 ([3054065](https://github.com/freire19/Alpha_Code/commit/3054065d8c2fec587b6f18edb64ca14c7f3e9b30))
* Gemini provider config + 1M context + OCR model env ([c9d1b91](https://github.com/freire19/Alpha_Code/commit/c9d1b9185689e290af4e3d4125f5634b9bdb5ad6))
* LLM fixture record/replay — H1 [#6](https://github.com/freire19/Alpha_Code/issues/6) ([51e7bd8](https://github.com/freire19/Alpha_Code/commit/51e7bd8079d74a954e5380780953827bef5421fa))
* session cost/token tracking + /cost — H1 [#4](https://github.com/freire19/Alpha_Code/issues/4) phase 1 ([2e23af7](https://github.com/freire19/Alpha_Code/commit/2e23af73b655d4af42f1ae75031e9dfe4a562022))


### Bug Fixes

* f-string backslash escapes break /cost and /stats import ([b84539d](https://github.com/freire19/Alpha_Code/commit/b84539d01671bdbf24f2a980ab1a87eff67dc38d))
* H1 quick wins — urllib3 CVE bump + path gating ([#001](https://github.com/freire19/Alpha_Code/issues/001), [#002](https://github.com/freire19/Alpha_Code/issues/002)) ([745fabe](https://github.com/freire19/Alpha_Code/commit/745fabeaf9b63320222966d1e2c8a938b9797a6f))


### Refactoring

* /simplify pass on H1 — perf + testability cleanups ([ac0df3f](https://github.com/freire19/Alpha_Code/commit/ac0df3f133a51c255162ba2bab5de84a17bf0a50))
* split display/core.py (1162 → 387 lines) — H1 [#3](https://github.com/freire19/Alpha_Code/issues/3) ([3465d6e](https://github.com/freire19/Alpha_Code/commit/3465d6e4fc37bb83da846c2d79672e4a55bf75bf))


### Documentation

* add Plano-Upgrade v1 + v3 (strategic roadmap) ([0bb3b9e](https://github.com/freire19/Alpha_Code/commit/0bb3b9e8b5483e0c5f5dd50729394570b29e920b))

## [1.8.7](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.8.6...alpha-code-v1.8.7) (2026-05-15)


### Bug Fixes

* classify questions vs tasks + assorted UX/robustness polish ([90de216](https://github.com/freire19/Alpha_Code/commit/90de21664054709b784a453de8c9f1f9644f0409))

## [1.8.6](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.8.5...alpha-code-v1.8.6) (2026-05-12)


### Bug Fixes

* zerar DEEP_BUGS, DEEP_LOGIC, e ALTOs do DEEP_RESILIENCE ([dc56b13](https://github.com/freire19/Alpha_Code/commit/dc56b13567101f957ec572b1075aea704931632e))

## [1.8.5](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.8.4...alpha-code-v1.8.5) (2026-05-12)


### Bug Fixes

* make todo_write always emit a visible inline line ([35f9bbd](https://github.com/freire19/Alpha_Code/commit/35f9bbd98b2551e562ff7001bf4230673c972048))

## [1.8.4](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.8.3...alpha-code-v1.8.4) (2026-05-12)


### Bug Fixes

* trigger release for simplify pass 3 patches ([0488c5a](https://github.com/freire19/Alpha_Code/commit/0488c5ac51093096f43cd8ab546e304f8c16af47))

## [1.8.3](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.8.2...alpha-code-v1.8.3) (2026-05-12)


### Bug Fixes

* **repl:** catch-all silent-turn marker, runs regardless of exit path ([7c6219c](https://github.com/freire19/Alpha_Code/commit/7c6219cb33972e283a632c018259f3572bac76f0))

## [1.8.2](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.8.1...alpha-code-v1.8.2) (2026-05-12)


### Bug Fixes

* audit findings DL035 / DL036 / DL037 ([820ebba](https://github.com/freire19/Alpha_Code/commit/820ebbaa12a4c0bb9e027137b4b52ec2c5a2919e))
* resilience + sub-agent hardening ([9fbddab](https://github.com/freire19/Alpha_Code/commit/9fbddabfd65c119ca09977d877eb7fb7b29f065d))

## [1.8.1](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.8.0...alpha-code-v1.8.1) (2026-05-12)


### Refactoring

* /simplify pass 2 — dedupe truncate/recovery, cap raw buffer ([73c1883](https://github.com/freire19/Alpha_Code/commit/73c18835322f3a4e37472907200d110a885cbded))

## [1.8.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.7.0...alpha-code-v1.8.0) (2026-05-11)


### Features

* **display:** Claude-Code-style diff — line numbers + full-width highlight ([5bd430f](https://github.com/freire19/Alpha_Code/commit/5bd430ff1c10204e121c7a36b15cd036ed861bb8))

## [1.7.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.6.2...alpha-code-v1.7.0) (2026-05-11)


### Features

* **display:** show tool target in spinner label, not just verb ([8cc4646](https://github.com/freire19/Alpha_Code/commit/8cc4646ff83591d704447b5b3da67a72715d0307))

## [1.6.2](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.6.1...alpha-code-v1.6.2) (2026-05-11)


### Bug Fixes

* **repl:** print marker on silent end-of-turn ([f81a08c](https://github.com/freire19/Alpha_Code/commit/f81a08c399d64664041cdd9e34926dd879780762))

## [1.6.1](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.6.0...alpha-code-v1.6.1) (2026-05-11)


### Bug Fixes

* **llm:** recover tool calls from DSML/XML invoke blocks (DeepSeek-V4-pro) ([c72de15](https://github.com/freire19/Alpha_Code/commit/c72de15b979affe5bbc2f0007b0b30e62822adc8))

## [1.6.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.5.3...alpha-code-v1.6.0) (2026-05-11)


### Features

* **agent,display,tools:** four runtime UX fixes from session screenshot ([940354d](https://github.com/freire19/Alpha_Code/commit/940354d3589648ffd0d89c83ba9f26b1dc81c61b))


### Bug Fixes

* **tests:** unbreak 12 post-refactor test failures ([710e8aa](https://github.com/freire19/Alpha_Code/commit/710e8aa2dbbba529cfbe65a69d13d4c53a5a6077))

## [1.5.3](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.5.2...alpha-code-v1.5.3) (2026-05-11)


### Bug Fixes

* import symbols stranded by tool/security splits ([c1d95e1](https://github.com/freire19/Alpha_Code/commit/c1d95e13fa72ccabc3bbb64b3d5103a78d0576bf))

## [1.5.2](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.5.1...alpha-code-v1.5.2) (2026-05-11)


### Bug Fixes

* nudge LLMs harder toward ask_choice instead of markdown lists ([3820147](https://github.com/freire19/Alpha_Code/commit/382014765bdbcdf5f5ecc6f4751ab39738a4b2df))

## [1.5.1](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.5.0...alpha-code-v1.5.1) (2026-05-11)


### Refactoring

* simplify findings — dedupe headers, kill dead stream path ([54afcfc](https://github.com/freire19/Alpha_Code/commit/54afcfcbf0a21c2e1256fd9543766b5c994cd77f))

## [1.5.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.4.0...alpha-code-v1.5.0) (2026-05-11)


### Features

* **tools:** add ask_choice tool + numbered menu renderer ([d639113](https://github.com/freire19/Alpha_Code/commit/d6391137a64176d4877adbf571ea468619d73cfa))

## [1.4.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.3.1...alpha-code-v1.4.0) (2026-05-11)


### Features

* **display:** solid color palette + aggregated Task block for delegate ([b203cad](https://github.com/freire19/Alpha_Code/commit/b203cada79b6cc8d5554bab1d6527f2aaf9d09e9))

## [1.3.1](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.3.0...alpha-code-v1.3.1) (2026-05-11)


### Bug Fixes

* **agent:** import _call_signature, _result_preview, _CYCLE_WINDOW from .loop ([38c73a8](https://github.com/freire19/Alpha_Code/commit/38c73a8bef75023826d21602006c6bfce1162abb))

## [1.3.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.2.3...alpha-code-v1.3.0) (2026-05-11)


### Features

* **display:** Claude-Code-style tool call/result/subagent rendering ([b7d342d](https://github.com/freire19/Alpha_Code/commit/b7d342ded53857b0661f93dc2e178dbb5cd79c2c))

## [1.2.3](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.2.2...alpha-code-v1.2.3) (2026-05-11)


### Bug Fixes

* **display:** keep cursor near prompt after scroll-region setup ([6398db5](https://github.com/freire19/Alpha_Code/commit/6398db5d6923c18e92e7f0edfced4f820082921d))

## [1.2.2](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.2.1...alpha-code-v1.2.2) (2026-05-11)


### Bug Fixes

* **display:** repair symbols stranded on wrong side of the split ([e29d3a8](https://github.com/freire19/Alpha_Code/commit/e29d3a81cc5e354c552b0e3a13d8678d2d601796))

## [1.2.1](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.2.0...alpha-code-v1.2.1) (2026-05-11)


### Bug Fixes

* **display:** add missing stdlib imports in thinking.py ([77b4ec4](https://github.com/freire19/Alpha_Code/commit/77b4ec48072e0a17f82dc6ed9c76a58732aaedbd))

## [1.2.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.1.0...alpha-code-v1.2.0) (2026-05-11)


### Features

* **bin:** add PowerShell updater for native Windows users ([7ce7ee7](https://github.com/freire19/Alpha_Code/commit/7ce7ee7350158337cb233aa2d5891ed165e3f998))
* live version from pyproject + improved completions + display aliases ([8d7e0ba](https://github.com/freire19/Alpha_Code/commit/8d7e0ba09924d759f2614d7cf20c5d484c15f176))
* **repl:** Claude-Code-style multi-line slash autocomplete ([c0e1d8d](https://github.com/freire19/Alpha_Code/commit/c0e1d8d0da47c97dfab8cd9f81a89a1f37b44d6f))
* **skills:** add 8 user-authored skills (audit suite, release, simplify, status) ([08c3487](https://github.com/freire19/Alpha_Code/commit/08c3487e780122349657dccd417a986b04869cfb))
* **skills:** add mvp-1-check and mvp-2-report ([9ed440f](https://github.com/freire19/Alpha_Code/commit/9ed440fc300a3c2a066f7a5d83180c617ab0ed18))
* Windows compat — colorama init, simple input fallback, cmd allowlist ([3885077](https://github.com/freire19/Alpha_Code/commit/3885077d635191c405ac70312a8ff3c34f5dcd86))


### Bug Fixes

* DEEP_BUGS zerado — 4 BAIXOs ([#004](https://github.com/freire19/Alpha_Code/issues/004), [#008](https://github.com/freire19/Alpha_Code/issues/008), [#017](https://github.com/freire19/Alpha_Code/issues/017), [#052](https://github.com/freire19/Alpha_Code/issues/052)) ([20a4bc4](https://github.com/freire19/Alpha_Code/commit/20a4bc4e41566b2fabe2daeb9ef194f28e889cd8))
* repair broken docstring in _format_result that blocked imports ([1cdcd94](https://github.com/freire19/Alpha_Code/commit/1cdcd947bd7839fbbfed971df43142ca01d7c2b4))
* REPL Windows — resposta invisivel quando spinner inline sobrescreve tokens ([2fd4299](https://github.com/freire19/Alpha_Code/commit/2fd429972acb82df041313a7f1248597781b45ac))
* **security:** MCP client now spawns with safe_env — no longer leaks API keys (#D115) ([19a5be0](https://github.com/freire19/Alpha_Code/commit/19a5be0c77bf451dc02c6ec4e7db7c9c2d75a0f6))
* skill YAML, release PR auto-merge, post-refactor cleanups ([6b846c3](https://github.com/freire19/Alpha_Code/commit/6b846c3f56c8995e59709141bad0d6caf696034a))


### Refactoring

* **agent:** split agent.py into agent/ package ([e49fba2](https://github.com/freire19/Alpha_Code/commit/e49fba23ea12c5342e69691be4cccdcd7d378823))
* **display:** split display.py into display/ package ([bc6b725](https://github.com/freire19/Alpha_Code/commit/bc6b725b8c1ad2d73498d0eb0a95f998f9880575))
* DM035 — move TOOL_TIMEOUTS imports to top-level in 6 tool modules ([0e373dd](https://github.com/freire19/Alpha_Code/commit/0e373ddc42f1ae5486c5bc61b1b433b7ed277dbd))
* DM036 — centralize retry config in config.RETRY dict ([260dade](https://github.com/freire19/Alpha_Code/commit/260dade6cff8c5f4b4194bf4fd5d16ef121afe42))
* DM041 — migrate category strings to ToolCategory enum ([83ef972](https://github.com/freire19/Alpha_Code/commit/83ef9723d1166fde3f6d7a4b80e5002899dc16ce))
* maintainability quick wins — 8 BAIXOs fechados ([b3eb317](https://github.com/freire19/Alpha_Code/commit/b3eb317919506a58ba776ec705e15c981e4e6452))
* simplify post-DEEP_PERFORMANCE — extract _quick_similar, deduplicate _SKIP_DIRS, fix stale docstring ([075cb0d](https://github.com/freire19/Alpha_Code/commit/075cb0d09eadd807c2bb59e02d11dcedd952dd5c))
* **tools:** extract security module, split browser registrations ([d8c69a8](https://github.com/freire19/Alpha_Code/commit/d8c69a84df27a81029ebdf821462fc15d95cd36b))

## [1.1.0](https://github.com/freire19/Alpha_Code/compare/alpha-code-v1.0.0...alpha-code-v1.1.0) (2026-05-08)


### Features

* /skills REPL command + slash autocomplete + skills audit tool ([0bfc40e](https://github.com/freire19/Alpha_Code/commit/0bfc40e6bd158e524ca36749e8b853dc284fb2a6))
* add browser automation tools and consolidate WIP ([f343706](https://github.com/freire19/Alpha_Code/commit/f343706f234f8dda1673b89c65ec9ff9bd08c275))
* add skills bundle, onboarding wizard, named agents, workspace isolation ([aabd7d8](https://github.com/freire19/Alpha_Code/commit/aabd7d8308300f9b949628147ff5888e5ad6a20a))
* ALPHA.md auto-load + /init + pre-commit secret guard ([82b2885](https://github.com/freire19/Alpha_Code/commit/82b2885820bf2bcf696c1d6b53feef898bc6e9f5))
* **attachments:** per-provider vision_format to fix DeepSeek image input ([79548f5](https://github.com/freire19/Alpha_Code/commit/79548f5d9396da2441b515c792cde8983064c20a))
* **bin:** add alpha-update wrapper for one-command upgrades ([cf0958f](https://github.com/freire19/Alpha_Code/commit/cf0958fea95246238e2dd1c4607ebd6193decdfb))
* default to DeepSeek V4 with 1M context detection ([6b2a0c9](https://github.com/freire19/Alpha_Code/commit/6b2a0c953f26e1da5dc5f77fe19b2332d1b28c33))
* gemini OCR fallback, inline edit diffs, async SSRF guard ([e9a35fb](https://github.com/freire19/Alpha_Code/commit/e9a35fbab3bad639c68d475f7a32e0dc4148c075))
* image attachments in REPL + adaptive context compression ([8a8e449](https://github.com/freire19/Alpha_Code/commit/8a8e449b25bb62ce9a5f77f39bdaf713a72f9bf9))
* MCP, hooks, plan/todo, Anthropic provider, lean profile, CI ([3702651](https://github.com/freire19/Alpha_Code/commit/3702651c44d92a7073795bcb05cc3a09fb48a755))
* **prompt:** harden ALPHA identity against misidentification ([37afa08](https://github.com/freire19/Alpha_Code/commit/37afa0807b330f8f36ab7c75424606c9dc4243af))
* **subagent:** configurable approval policy via FEATURES + env ([42fb56a](https://github.com/freire19/Alpha_Code/commit/42fb56a2b7c71a05482b29672439405ed2278db8))
* **ui:** context-window feedback + cleaner tool-call rendering ([550eecb](https://github.com/freire19/Alpha_Code/commit/550eecb28da1d5a35586ea7243f20ea6beb3598b))
* **ui:** dynamic phase labels in thinking indicator ([3dfffaf](https://github.com/freire19/Alpha_Code/commit/3dfffaf880256dfd0926adf6c29dd08f4cf59fea))


### Bug Fixes

* 4 cross-loop / cross-context bugs + ReDoS heuristic + cleanup ([8a37f1e](https://github.com/freire19/Alpha_Code/commit/8a37f1eb25f778c19333ff37615b0d8097414ee1))
* AUDIT_V1.2 batch — concurrency, context, sandbox guards ([0eb9699](https://github.com/freire19/Alpha_Code/commit/0eb96997ef257a64418b9a7d2994159613c0caa2))
* backfill tool messages on interrupt + harden DNS rebinding mitigation ([cffdb6c](https://github.com/freire19/Alpha_Code/commit/cffdb6ce5e10593d634e1a7cadaa4524db4a191f))
* **bugs:** close 4 MEDIOs from DEEP_BUGS V1.1 ([a224291](https://github.com/freire19/Alpha_Code/commit/a2242912054cc807d4683e3eb860194b18c1c3bd))
* **bugs:** close all 9 DEEP_BUGS V1.0 stragglers (#D022–#D030) ([db75078](https://github.com/freire19/Alpha_Code/commit/db75078bf35504d0be784288be757cb7c48e9a67))
* close last 7 V2.0 stragglers across security, resilience, logic ([82f1291](https://github.com/freire19/Alpha_Code/commit/82f129165e0e83a5beafcb128fdd8258ada8359b))
* **config:** mark deepseek provider as supports_vision=True ([684373f](https://github.com/freire19/Alpha_Code/commit/684373fcc06770f0e2a7331c8ce370d76d88dd79))
* **config:** revert deepseek supports_vision to False ([486b1ff](https://github.com/freire19/Alpha_Code/commit/486b1ffffdd6afa76892dda04e70c5536bd657ff))
* **deps:** pin lxml&gt;=6.1.0 to close CVE-2026-41066 ([d86d45c](https://github.com/freire19/Alpha_Code/commit/d86d45cc18365735e08dbc9775847c9f1b750a02))
* DL batch — workspace helper consolidation, error-shape uniformity ([288526c](https://github.com/freire19/Alpha_Code/commit/288526c0674537d81f1b6fe89ad8a0eb6fedf5a9))
* **history:** block path traversal via crafted session_id ([56d65ab](https://github.com/freire19/Alpha_Code/commit/56d65abaabbd46d7ce6314910ea4a80127bc3376))
* **llm:** preserve reasoning_content for DeepSeek thinking mode ([ded9530](https://github.com/freire19/Alpha_Code/commit/ded9530befbf2abe51b281412c53bb38445ca117))
* **logic:** close 4 ALTOs from DEEP_LOGIC V1.1 ([0424f3c](https://github.com/freire19/Alpha_Code/commit/0424f3cb262637ed4dea72dcdd087f910b4a24f0))
* **logic:** close 4 MEDIOs from DEEP_LOGIC V1.1 ([fb26eb4](https://github.com/freire19/Alpha_Code/commit/fb26eb40eb3745cb5fce66c71396956c7b69fedc))
* **resilience:** close 7 MEDIOs from DEEP_RESILIENCE V2.0 ([093accd](https://github.com/freire19/Alpha_Code/commit/093accdb150622f15d0f5ddd14f681a10c41806a))
* **resilience:** close 7 more DEEP_RESILIENCE V1.0/V1.1 stragglers ([006ddf3](https://github.com/freire19/Alpha_Code/commit/006ddf3854d88ac0d8a50165712cf0206a9c2d5c))
* **resilience:** close 9 DEEP_RESILIENCE V1.0/V1.1 stragglers ([c1c92f1](https://github.com/freire19/Alpha_Code/commit/c1c92f1facf29ba32c6412ce927dbdeed4087802))
* **resilience:** hard-truncate fallback when compression LLM fails ([b9a4e7c](https://github.com/freire19/Alpha_Code/commit/b9a4e7cae89a3ff5af8152d8b6d1f9597e7c7d62))
* **security:** close 4 ALTOs from DEEP_SECURITY V2.0 ([26a0c03](https://github.com/freire19/Alpha_Code/commit/26a0c031441f52570cc8c0cd6e656e64247e0909))
* **security:** close 4 MEDIOs from DEEP_SECURITY V2.0 ([156e9c0](https://github.com/freire19/Alpha_Code/commit/156e9c05211cb92f5ecb6048acb6c02b4bd97bf4))
* **security:** close 4 more DEEP_SECURITY V1.0/V1.1 stragglers ([d1d6ba5](https://github.com/freire19/Alpha_Code/commit/d1d6ba5522eb772807190aea818995588a505a48))
* **security:** close 7 DEEP_SECURITY V1.0/V1.1 stragglers ([99637f4](https://github.com/freire19/Alpha_Code/commit/99637f48cdc58d815e82a6b920c2df2e76beebd1))
* **security:** close CRITICO [#009](https://github.com/freire19/Alpha_Code/issues/009) — execute_python AST bypass via low-level OS modules ([f142d23](https://github.com/freire19/Alpha_Code/commit/f142d2316357b3aad4906bb76091120e563c1aa5))
* **security:** close sub-agent escape via browser tools and git write ([799dc50](https://github.com/freire19/Alpha_Code/commit/799dc50e3e18cdf28056b0287f49a38fc9bcf688))
* **security:** hardening pass + provider vision guard ([906ec1c](https://github.com/freire19/Alpha_Code/commit/906ec1c209008631693f6afe61707b2f87d95e34))
* **security:** require approval for clipboard_read (DEEP_SECURITY #D103) ([c9ca249](https://github.com/freire19/Alpha_Code/commit/c9ca2494c9a300e23807822bbf223fc1bd4f0bca))
* simplify batch — retry/atomic-write/IO hardening + spinner polish ([6c2fece](https://github.com/freire19/Alpha_Code/commit/6c2fece3b82c1e911993c00ef90204ed3de0f69e))
* stable recovered tool_call ids + cross-thread regex timeout ([d74e31e](https://github.com/freire19/Alpha_Code/commit/d74e31e212ddef6a25dc2759a446351fb42bc56f))


### Performance

* **agent:** strip common prefix in loop-detection similarity check ([9683ae1](https://github.com/freire19/Alpha_Code/commit/9683ae18d48daf762ab7637d53ee22c4576e1b05))
* close 3 ALTOs from DEEP_PERFORMANCE V2.0 ([c4dc55b](https://github.com/freire19/Alpha_Code/commit/c4dc55b1f75574213315b885293a31728630c4b2))
* close 7 MEDIOs from DEEP_PERFORMANCE V2.0 ([471ca22](https://github.com/freire19/Alpha_Code/commit/471ca221034630ccbef15f23d183f54b3b4b0550))
* **search:** migrate search_files to ripgrep with python fallback ([07e954c](https://github.com/freire19/Alpha_Code/commit/07e954cd78310d05e86cdd3cdd6a58c0e93f39c3))
* single-pass _format_result with per-field preview clipping ([95bd15e](https://github.com/freire19/Alpha_Code/commit/95bd15e70886b6b3b5139324606a810b80c8cbb6))


### Refactoring

* **cli:** camada 3 — security + structure cleanup ([27f4dc5](https://github.com/freire19/Alpha_Code/commit/27f4dc5c343c61ab779d9f3ee915f1229b0d5f2c))
* **cli:** dispatch table for slash commands + REPL integration tests ([00fe291](https://github.com/freire19/Alpha_Code/commit/00fe2916cd7876ca659fc9d459226811d622af94))
* close 7 of 8 MEDIOs from DEEP_MAINTAINABILITY V1.1 ([37aa292](https://github.com/freire19/Alpha_Code/commit/37aa2924283719d9977cc37707c42ec75af6f969))
* consolidate TOOL_TIMEOUTS + fix pipeline FD leak ([33c1793](https://github.com/freire19/Alpha_Code/commit/33c1793bf046b82da1d238e6e5aef6649eaca8c2))
* **display:** tools list reads category from registry ([f706de6](https://github.com/freire19/Alpha_Code/commit/f706de6d309c7d2e893d4e651264b6c1bbd01d2c))
* **security:** drop _extract_relevant_context (dead code + injection vector) ([85a3e79](https://github.com/freire19/Alpha_Code/commit/85a3e791a1496e2240ca2b0e033f31def58e6e86))
* split composite/delegate tools into focused helpers + rate limiter ([6500cf8](https://github.com/freire19/Alpha_Code/commit/6500cf88348b4b2b11f1e355dc063496bc924c1a))


### Documentation

* add MIT license and minimal README ([b1f65e4](https://github.com/freire19/Alpha_Code/commit/b1f65e44a025274204a91c985ad1f2742228e38e))
* correct ALTO pendentes counter (2 -&gt; 4) ([eca5fbb](https://github.com/freire19/Alpha_Code/commit/eca5fbb7aaac1e30fa687e4ff0d2ee20865ecc3d))
* **readme:** add Update section after Install ([78b566d](https://github.com/freire19/Alpha_Code/commit/78b566d1af28098e15e2f818e80b6f760830f53e))
* refresh STATUS to reflect verified state (0 critical, 7 ALTO open) ([7708deb](https://github.com/freire19/Alpha_Code/commit/7708debb43786fb132f4fb89d8945e6897af2469))
* **user-guide:** add Skills section explaining locations and authoring ([7e0a9ee](https://github.com/freire19/Alpha_Code/commit/7e0a9ee7c033732d8dd20ea90653c1376cb397af))
