import sys
sys.path.insert(0, "/home/yasu/stackchan-server")
import server_tools
names = ["度会隆輝", "渡会", "石上泰輝", "林琢真", "京田陽太", "牧秀悟", "森敬斗",
         "佐野恵太", "神里和毅", "戸柱恭孝", "筒香嘉智", "柴田竜拓", "宮﨑敏郎",
         "梶原昂希", "関根大気", "捕手のテーマ", "その他の左打者"]
for n in names:
    print("%s -> %s" % (n, server_tools._yomi(n)))
