# Multi-conversation bench summary

**Generated:** 2026-05-17_132443

**Setup:** 5 conversations × 5 turns, ~10000-token doc shared across convs,
max_new_tokens=50, server restart between every turn (cross-process scenario).

## TTFT median by turn position

| turn | native median (ms) | wombatkv median (ms) | speedup |
|---|---:|---:|---:|
| 1 | 114716 | 2421 | 47.4× |
| 2 | 103675 | 1568 | 66.1× |
| 3 | 123706 | 2131 | 58.1× |
| 4 | 106670 | 1812 | 58.9× |
| 5 | 99919 | 1753 | 57.0× |

## Overall TTFT median

- ds4-native: 110535 ms
- ds4 + WombatKV: 1883 ms
- **Speedup: 58.7×**
