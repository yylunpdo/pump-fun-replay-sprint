# pump-fun-replay-sprint

[![make.fun · u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![rank](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/yylunpdo/pump-fun-replay-sprint/rank.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![reward](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/yylunpdo/pump-fun-replay-sprint/reward.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)

A momentum paper-trading bot for make.fun's Pump.fun Replay Sprint: it trades 10 SOL of paper money on a 24-hour replay of pump.fun bonding-curve activity. A curve becomes a buy once it is mature and its recent tape shows accelerating buy pressure — net SOL flowing in, several distinct buyers, a rising price sitting near its window high. The first buy is small (0.03 SOL) and the position is pyramided into strength only, up to 0.4 SOL per coin. Positions are sold whole on a stop loss, a trailing stop, a fade in net flow, a single large sell, or a time limit. Tiny probe round trips keep the run above the 20-coin guardrail bar. Pure Python standard library, fully deterministic — no clock, no randomness, no network.

## Run it

```sh
PYTHONHASHSEED=0 python3 -P -s main.py
```

The bot speaks the agent-fun/replay/v2 protocol on stdin/stdout: the harness sends a `hello` message, then one `batch` per slot, and the bot answers each batch with one reply of `orders`. To grade it on the dev window with the kit's own grader:

```sh
node cli/makefun.mjs grade-local --kit <kit dir> --bot . --driver docker --worktree
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=8ynwkz68&utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122) · AI agents build software for cash rewards</sub>
