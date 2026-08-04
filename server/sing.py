"""歌わせる。音符と歌詞から楽譜（MusicXML）を書いて Sinsy に渡し、音を返す。

Sinsy は名古屋工業大学の HMM 歌声合成（Modified BSD）。声は同大の
`nitech_jp_song070_f001`（Creative Commons Attribution 3.0）。どちらも
Raspberry Pi では組まず、GitHub Actions で aarch64 向けに作ったものを置いてある
（`.github/workflows/build-sinsy.yml`）。

音符の書き方（1 音 = 3 つ組）:
    ["ド4", 4, "か"]   音の高さ / 長さ（16分音符いくつ分）/ その音で歌う一文字
    [null, 2, ""]      休み
高さは "ド4" のような日本語表記でも "C4" でも書ける（4 = ピアノの中央のオクターブ）。

  ./.venv/bin/python sing.py            # 動作確認（唱歌「ふるさと」の出だし）
"""
import asyncio
import os
import subprocess
import tempfile
import wave

SINSY_BIN = os.environ.get("SINSY_BIN", os.path.expanduser("~/sinsy/sinsy"))
SINSY_DIC = os.environ.get("SINSY_DIC", os.path.expanduser("~/sinsy/dic"))
SINSY_VOICE = os.environ.get(
    "SINSY_VOICE",
    os.path.expanduser("~/sinsy/htsvoice/nitech_jp_song070_f001.htsvoice"))
SINSY_TIMEOUT = float(os.environ.get("SINSY_TIMEOUT", "120"))

# 日本語の音名。「ド4」＝ 4 オクターブ目のド
_JP_STEP = {"ド": ("C", 0), "ド#": ("C", 1), "レb": ("D", -1), "レ": ("D", 0),
            "レ#": ("D", 1), "ミb": ("E", -1), "ミ": ("E", 0), "ファ": ("F", 0),
            "ファ#": ("F", 1), "ソb": ("G", -1), "ソ": ("G", 0),
            "ソ#": ("G", 1), "ラb": ("A", -1), "ラ": ("A", 0),
            "ラ#": ("A", 1), "シb": ("B", -1), "シ": ("B", 0)}

# 16分音符いくつ分か → 音符の名前と付点の有無
_TYPES = {1: ("16th", 0), 2: ("eighth", 0), 3: ("eighth", 1),
          4: ("quarter", 0), 6: ("quarter", 1), 8: ("half", 0),
          12: ("half", 1), 16: ("whole", 0)}
# 楽譜に書ける長さ（長い順）。書けない長さはこれらに分けてタイでつなぐ
_WRITABLE = sorted(_TYPES, reverse=True)


def parse_pitch(name):
    """"ド4" / "C4" / "ファ#3" を（音名, 半音のずれ, オクターブ）にする。"""
    if name is None:
        return None
    s = str(name).strip()
    octave = int(s[-1])
    head = s[:-1]
    if head in _JP_STEP:
        step, alter = _JP_STEP[head]
        return step, alter, octave
    step = head[0].upper()
    alter = {"#": 1, "b": -1}.get(head[1:2], 0)
    if step not in "ABCDEFG":
        raise ValueError("音の高さが読めません: %r" % name)
    return step, alter, octave


def _split_len(length, room):
    """1 音を、楽譜に書ける長さの並びに分ける（小節をまたぐ時も使う）。"""
    out = []
    while length > 0:
        take = 0
        for w in _WRITABLE:
            if w <= min(length, room):
                take = w
                break
        if not take:
            # 16分音符より短い端数は捨てる（そこまで細かい音は書かない）
            break
        out.append(take)
        length -= take
        room -= take
        if room <= 0:
            break
    return out, length


def _note_xml(pitch, length, lyric, tie):
    """1 音ぶんの XML。tie は (つなぎ始め, つなぎ終わり) の 2 つ。

    Sinsy が受け取れるのは start と stop だけで、真ん中の音に continue と書くと
    「<tie> tag has unexpected attribute(type)」で読み込みごと落ちる。3 つ以上に
    割れた音の真ん中は、stop と start を両方書く。
    """
    start, stop = tie
    x = ["      <note>"]
    if pitch is None:
        x.append("        <rest/>")
    else:
        step, alter, octave = pitch
        x.append("        <pitch>")
        x.append("          <step>%s</step>" % step)
        if alter:
            x.append("          <alter>%d</alter>" % alter)
        x.append("          <octave>%d</octave>" % octave)
        x.append("        </pitch>")
    x.append("        <duration>%d</duration>" % length)
    # ここに <tie type="..."/> は書かない。Sinsy の読み込みが
    # 「<tie> tag has unexpected attribute(type)」で落ちる（小節をまたぐ長い音で
    # 初めて出た）。つなぎは下の <notations><tied> だけで足りる
    kind, dotted = _TYPES[length]
    x.append("        <voice>1</voice>")
    x.append("        <type>%s</type>" % kind)
    if dotted:
        x.append("        <dot/>")
    if start or stop:
        x.append("        <notations>")
        if stop:
            x.append('          <tied type="stop"/>')
        if start:
            x.append('          <tied type="start"/>')
        x.append("        </notations>")
    if lyric:
        x.append("        <lyric>")
        x.append("          <syllabic>single</syllabic>")
        x.append("          <text>%s</text>" % lyric)
        x.append("        </lyric>")
    x.append("      </note>")
    return x


def musicxml(notes, tempo=120, beats=4, beat_type=4, title="song"):
    """音符の並びから MusicXML を組み立てる。長さの単位は 16分音符。"""
    per_measure = beats * 16 // beat_type
    body, measure, room = [], [], per_measure
    number = 1

    def open_measure():
        head = ['    <measure number="%d">' % number]
        if number == 1:
            head += ["      <attributes>",
                     "        <divisions>4</divisions>",
                     "        <key><fifths>0</fifths></key>",
                     "        <time><beats>%d</beats>"
                     "<beat-type>%d</beat-type></time>" % (beats, beat_type),
                     "        <clef><sign>G</sign><line>2</line></clef>",
                     "      </attributes>",
                     '      <direction placement="above">',
                     "        <direction-type><metronome>"
                     "<beat-unit>quarter</beat-unit>"
                     "<per-minute>%d</per-minute>"
                     "</metronome></direction-type>" % tempo,
                     '        <sound tempo="%d"/>' % tempo,
                     "      </direction>"]
        return head

    measure = open_measure()
    for raw in notes:
        name, length, lyric = raw[0], int(raw[1]), (raw[2] if len(raw) > 2 else "")
        pitch = parse_pitch(name)
        first = True
        while length > 0:
            pieces, length = _split_len(length, room)
            if not pieces:
                break
            for i, piece in enumerate(pieces):
                more = (length > 0) or (i < len(pieces) - 1)
                if pitch is None:
                    tie = (False, False)
                else:
                    tie = (more, not first)
                measure += _note_xml(pitch, piece,
                                     lyric if first else "", tie)
                room -= piece
                first = False
                if room <= 0:
                    measure.append("    </measure>")
                    body += measure
                    number += 1
                    room = per_measure
                    measure = open_measure()
    if room < per_measure:
        # 最後の小節が余っていたら休みで埋める
        pieces, _ = _split_len(room, room)
        for piece in pieces:
            measure += _note_xml(None, piece, "", (False, False))
        measure.append("    </measure>")
        body += measure

    return "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE score-partwise PUBLIC '
        '"-//Recordare//DTD MusicXML 3.0 Partwise//EN" '
        '"http://www.musicxml.org/dtds/partwise.dtd">',
        '<score-partwise version="3.0">',
        "  <work><work-title>%s</work-title></work>" % title,
        "  <part-list>",
        '    <score-part id="P1"><part-name>%s</part-name></score-part>' % title,
        "  </part-list>",
        '  <part id="P1">'] + body + ["  </part>", "</score-partwise>", ""])


def _run_sinsy(xml_path, wav_path):
    return subprocess.run(
        [SINSY_BIN, "-x", SINSY_DIC, "-m", SINSY_VOICE, "-o", wav_path,
         xml_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=SINSY_TIMEOUT)


def sing_sync(notes, tempo=120, beats=4, beat_type=4, title="song"):
    """歌わせて (pcm, サンプリング周波数) を返す。pcm は 16bit モノラル。"""
    xml = musicxml(notes, tempo, beats, beat_type, title)
    d = tempfile.mkdtemp(prefix="sing-")
    xml_path = os.path.join(d, "score.xml")
    wav_path = os.path.join(d, "out.wav")
    try:
        with open(xml_path, "w", encoding="utf-8") as f:
            f.write(xml)
        r = _run_sinsy(xml_path, wav_path)
        if not os.path.exists(wav_path):
            raise RuntimeError("歌えませんでした: %s"
                               % r.stdout.decode("utf-8", "replace")[-300:])
        with wave.open(wav_path, "rb") as w:
            return w.readframes(w.getnframes()), w.getframerate()
    finally:
        for p in (xml_path, wav_path):
            if os.path.exists(p):
                os.remove(p)
        os.rmdir(d)


async def sing(notes, **kw):
    return await asyncio.to_thread(sing_sync, notes, **kw)


# 唱歌「ふるさと」（岡野貞一・1914 年／作曲者は 1941 年没＝日本では著作権消滅）の
# 出だし。合成の道具立てが通っているかを確かめるためだけに使う
FURUSATO = [
    ["ミ4", 2, "う"], ["ミ4", 2, "さ"], ["ソ4", 4, "ぎ"],
    ["ソ4", 2, "お"], ["ラ4", 2, "い"], ["ソ4", 4, "し"],
    ["ミ4", 2, "か"], ["ミ4", 2, "の"], ["レ4", 4, "や"],
    ["ド4", 4, "ま"], [None, 4, ""],
]

if __name__ == "__main__":
    pcm, rate = sing_sync(FURUSATO, tempo=104, title="ふるさと")
    peak = max(abs(int.from_bytes(pcm[i:i + 2], "little", signed=True))
               for i in range(0, len(pcm), 2))
    print("%.2f 秒 / %d Hz / いちばん大きい振幅 %d"
          % (len(pcm) / 2 / rate, rate, peak))
