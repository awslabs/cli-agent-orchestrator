# Spike 3 Result

- Verdict: **GO**
- Recommended interval: `500 ms`

## Measurements

| Interval | First detection (ms) | CPU % | Poll count | Miss count |
|---|---:|---:|---:|---:|
| 100 ms | 152.7 | 2.04 | 23 | 0 |
| 200 ms | 207.3 | 3.64 | 13 | 0 |
| 500 ms | 144.2 | 0.83 | 16 | 0 |

## Raw JSON
```json
[
  {
    "interval_ms": 100,
    "first_detection_ms": 152.7,
    "cpu_percent": 2.04,
    "polls": 23,
    "miss_count": 0
  },
  {
    "interval_ms": 200,
    "first_detection_ms": 207.3,
    "cpu_percent": 3.64,
    "polls": 13,
    "miss_count": 0
  },
  {
    "interval_ms": 500,
    "first_detection_ms": 144.2,
    "cpu_percent": 0.83,
    "polls": 16,
    "miss_count": 0
  }
]
```
