# wifi-csi-respiration

## 설치

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 보드

| 항목        | 값                                                                           |
| ----------- | ---------------------------------------------------------------------------- |
| 대역 / 채널 | 5 GHz, 채널 36 (모든 링크 같음)                                              |
| 대역폭      | HT20 (`LINK_WIFI_BANDWIDTH_MHZ 20`) → CSI 57빈                               |
| 전송        | ESP-NOW 유니캐스트, 802.11n MCS0 long GI, 10 ms 주기(약 100패킷/s)           |
| 송신 전력   | 2 dBm (`TX_MAX_TX_POWER_QUARTER_DBM 8`, 케이스 안 직접 결합을 줄이려고 조정) |

## 사용

메인 보드 USB(`/dev/ttyUSB*`)를 연결한다.

```bash
# 기록만
.venv/bin/python tools/collect.py record --port /dev/ttyUSB0

# 기록 + 실시간 판정
.venv/bin/python tools/collect.py live --port /dev/ttyUSB0

# 기록 + 숨 참기 시험 (긴 삐 = 참기, 짧은 삐 = 쉬기)
.venv/bin/python tools/collect.py hold --port /dev/ttyUSB0
```

- 끝내기: Ctrl+C 또는 `--duration <초>`.
- 기록을 끄려면 `--no-record`. 출력은 `recordings/` 아래.
- 옵션 전체: `tools/collect.py --help`

## 참고

- 무호흡: C. Jung, Y. Yoo, H.-W. Kim, H.-C. Shin, "Detecting Sleep-Related Breathing Disorders Using FMCW Radar", J. Electromagn. Eng. Sci. 23(5):437–445, 2023. doi:10.26866/jees.2023.5.r.189
- 호흡수 측정: H. Wang, D. Zhang, J. Ma, Y. Wang, Y. Wang, D. Wu, T. Gu, B. Xie, "Human Respiration Detection with Commodity WiFi Devices: Do User Location and Body Orientation Matter?", UbiComp 2016.
