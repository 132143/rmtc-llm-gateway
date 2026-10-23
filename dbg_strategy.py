import json
from contestant.sim import Stage1Map, run_stage1
from contestant.strategy import GreedyBfsStrategy

cfg = json.load(open("docs/practice_stage1.json", encoding="utf-8"))
m = Stage1Map.from_config(cfg)
s = GreedyBfsStrategy(m.width, m.height, 1.0)

# 手动跑 30 Tick，打印每步决策
import contestant.sim as sim
pos = m.spawn; obstacles = set(m.obstacles); remaining = dict(m.resources); explored={pos}
last_blocked=set()
for tick in range(30):
    visible=[(p,v) for p,v in remaining.items() if abs(p[0]-pos[0])<=4 and abs(p[1]-pos[1])<=4]
    local={p for p in obstacles if abs(p[0]-pos[0])<=4 and abs(p[1]-pos[1])<=4}
    v = sim.View(pos, visible, local, explored, set(), last_blocked, tick, 4)
    s.observe(v)
    a = s.decide(v)
    print(tick, pos, a, "seen:", len(s.seen_resources), "blocked:", list(s.blocked)[:3])
    last_blocked=set()
    if a.get("action")=="move":
        dx,dy = sim.DIRECTIONS[a["direction"]]
        nxt=(pos[0]+dx,pos[1]+dy)
        if 0<=nxt[0]<m.width and 0<=nxt[1]<m.height and nxt not in obstacles:
            pos=nxt; explored.add(pos)
        else:
            last_blocked.add(nxt)
    if pos in remaining: remaining.pop(pos)
