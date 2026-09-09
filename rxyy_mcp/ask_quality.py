# -*- coding: utf-8 -*-
"""zhi 提问的选项闸 —— 把「没法选的选项集」拦在 rxyy 眼前之外。

08-24 rxyy 原话：「有时候 agent 会提问：一堆选项，有时候我都不知道怎么选，后果
是什么……给一堆雷同的或者给一堆其实那些都需要做的选项，就很容易让我摸不着头脑。
理论上按正确的最优的选项给、解决、去做就行了。」

光改提示词不够——同一条纪律 08-07 就写进 instructions 了，当天的提问照样是六到
七条。所以这里把「什么样的选项集不该送出去」写成可判定的规则，只认三种形状，
每一种都能从当天的真实提问里举出例子（见 tests/test_ask_quality.py 的语料）：

1. 伞形选项——列表里有「都干 / 全部 5 批都认领 / 两个洞都堵」这类把好几条并起来
   的选项。它一出现就说明其余各条本来不互斥，是同一批待办被拆开摆着让人挑。
   这种不是选择题，直接全做。
2. 雷同——两条归一化后几乎是同一句话，或者短的整个包在长的里面。
3. 太多——真正要动手的选项超过 MAX_ACTIONABLE 条（「结束」这类收尾项不计）。

拦下来的做法刻意留了后门：**同一个会话在冷却期内只拦一次**。第二次再来就放行，
宁可让 rxyy 看见一次不完美的提问，也绝不能因为这道闸把人问不到、把会话卡死。
"""
from __future__ import annotations

import re
import time
from difflib import SequenceMatcher

# 真正要动手的选项最多几条。再多就是让人做阅读理解，不是让人做决定。
MAX_ACTIONABLE = 4

# 两条选项像到什么程度才算雷同。0.88 是拿真实提问校准出来的下限：08-07 那组
# 「推送 X 顺手收 BOM／只推送 X，BOM 不管／先别推，只修 BOM 一起提」实测 0.5~0.8，
# 它们是三条真正互斥的路，绝不能被判成雷同。
DUP_RATIO = 0.88
# 参与比对的最短长度：「推送」「结束」这种短选项天然长得像。
DUP_MIN_LEN = 8

# 同一会话被拦下后多久内不再拦（秒）。这是防死锁的安全阀，不是给 agent 的旁路。
BOUNCE_COOLDOWN = 180.0

# 收尾/终止项不算「要动手的选项」——「结束」永远该在，不该占额度。
_TERMINAL_RE = re.compile(r"^(结束|收工|完事|没了|不用了|就这样)")

# 归一化只留中日韩汉字与字母数字，标点、空白、emoji 全丢掉。
_KEEP_RE = re.compile(r"[0-9a-zA-Z\u4e00-\u9fff]+")

# 「都」后面直接跟动词 = 一条选项里打包了好几件事
_ALL_VERB_RE = re.compile(r"都(干|做|改|堵|提|清|删|认领|处理|办|修|上|来|要做|得做)")
_ALL_WORD_RE = re.compile(r"全都|统统|全部都")
# 光有「都」还不够，得看出它在数不止一件事：加号、顿号、以及，或量词短语
_MULTI_RE = re.compile(
    r"[+＋、]|以及"
    r"|(两|三|四|五|六|七|八|九|十)(个|件|条|批|家|处|笔|份|种|项|块)"
    r"|全部|所有"
    r"|[2-9]\d*\s*(个|件|条|批|家|处|笔|份|种|项|块)"
)
# 光杆「都干」没东西可数，但它就是伞形选项本身
_BARE_ALL = {"都干", "都做", "全干", "全做", "都要", "全都干", "全都做"}

_last_bounce = {}


def normalize(text):
    """比对用的裸文本：只留汉字与字母数字。"""
    return "".join(_KEEP_RE.findall(str(text or ""))).lower()


def is_terminal(option):
    """「结束」这类收尾项：不占选项额度，也永远不算伞形。"""
    return bool(_TERMINAL_RE.match(normalize(option)))


def is_umbrella(option):
    """这一条是不是「把上面几条并起来一起做」。"""
    raw = str(option or "").strip()
    if not raw or is_terminal(raw):
        return False
    if normalize(raw) in _BARE_ALL:
        return True
    if not (_ALL_VERB_RE.search(raw) or _ALL_WORD_RE.search(raw)):
        return False
    return bool(_MULTI_RE.search(raw))


def _similar(a, b):
    return SequenceMatcher(None, a, b).ratio()


def find_duplicates(options):
    """返回雷同的下标对（1 开始，方便直接写进给 agent 的话）。"""
    norm = [normalize(o) for o in options]
    pairs = []
    for i in range(len(norm)):
        for j in range(i + 1, len(norm)):
            a, b = norm[i], norm[j]
            if len(a) < DUP_MIN_LEN or len(b) < DUP_MIN_LEN:
                continue
            short, long_ = (a, b) if len(a) <= len(b) else (b, a)
            if short in long_ or _similar(a, b) >= DUP_RATIO:
                pairs.append((i + 1, j + 1))
    return pairs


def review(options):
    """看一眼这组选项能不能让人选得动。返回问题清单（空 = 放行）。"""
    opts = [str(o) for o in (options or []) if str(o).strip()]
    if len(opts) < 2:
        return []

    problems = []

    umbrellas = [i + 1 for i, o in enumerate(opts) if is_umbrella(o)]
    if umbrellas:
        which = "、".join("第%d条" % i for i in umbrellas)
        problems.append(
            "%s是「都干」式的伞形选项——它在场就说明其余各条本来不互斥，"
            "是同一批待办被拆开摆着让 rxyy 挑。这不是选择题。" % which
        )

    actionable = [o for o in opts if not is_terminal(o)]
    if len(actionable) > MAX_ACTIONABLE:
        problems.append(
            "要动手的选项 %d 条，超过 %d 条上限（「结束」不算）——一眼看不完就选不动。"
            % (len(actionable), MAX_ACTIONABLE)
        )

    dups = find_duplicates(opts)
    if dups:
        which = "、".join("第%d条与第%d条" % p for p in dups)
        problems.append("%s几乎是同一句话，摆两遍只会让人以为漏看了区别。" % which)

    return problems


def bounce_message(problems):
    """退回给 agent 的话：说清拦在哪，并且给出两条明确出路。"""
    lines = [
        "🚦【选项闸 · 本次提问没有送出去】",
        "",
        "rxyy 08-24 立的规矩：能自己判定最优解就直接做完再报，别把待办摆成选项让他挑。"
        "他的原话是「按正确的最优的选项给、解决、去做就行了」。",
        "",
        "这次拦下的理由：",
    ]
    lines += ["- " + p for p in problems]
    lines += [
        "",
        "接下来二选一：",
        "① 最优解其实已经清楚（几件事都得做、或者只有一条路是对的）→ 别问了，"
        "直接做完，再用 zhi 报结果，改了 bug/UI 就用「成果」带上 diff 或截图。",
        "② 确实是取舍要 rxyy 定夺（不可逆、涉及他的口味或优先级）→ 重发 zhi："
        "选项互相排斥、每条一句话写明选了会怎样、最多 %d 条，不要「都干」那条。" % MAX_ACTIONABLE,
    ]
    return "\n".join(lines)


def bounce_text(options, conv_key="", now=None):
    """给 server 用的入口：该拦就返回退回话术，不该拦返回 None。

    冷却期内同一会话只拦一次——这道闸宁可放一次不完美的提问过去，
    也绝不能让 rxyy 问不到人。
    """
    problems = review(options)
    if not problems:
        return None
    ts = time.time() if now is None else now
    key = conv_key or "__default__"
    if ts - _last_bounce.get(key, 0.0) < BOUNCE_COOLDOWN:
        return None
    _last_bounce[key] = ts
    _prune(ts)
    return bounce_message(problems)


def _prune(now):
    """别让长期不回来的会话把这张表撑大。"""
    if len(_last_bounce) <= 256:
        return
    for k, ts in list(_last_bounce.items()):
        if now - ts > BOUNCE_COOLDOWN * 4:
            _last_bounce.pop(k, None)
