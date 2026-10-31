import io

P = "/home/yasu/stackchan-server/e2e_real_llm.sh"
src = io.open(P, encoding="utf-8").read()
old = "export SAKURA_MODEL=qwen2.5:3b\n"
new = 'export SAKURA_MODEL="${SAKURA_MODEL:-qwen2.5:3b}"\n'
assert src.count(old) == 1, src.count(old)
io.open(P, "w", encoding="utf-8").write(src.replace(old, new))
print("ok")
