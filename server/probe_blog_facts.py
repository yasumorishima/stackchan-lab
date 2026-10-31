import server_tools as t
names = [x["function"]["name"] for x in t.SPECS]
print("SERVER_TOOLS", len(names))
print(" ".join(names))
