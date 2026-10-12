# wifi-csi-respiration

## 설치

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 보드

- 방마다 케이스 하나(송신기 csi-tx + 수신기 csi-rx), 방 밖에 메인 보드(csi-main). 모두 ESP32-C5.
- 송신기·수신기는 전원만 있으면 된다. **메인 보드의 UART USB(CP2102N, `/dev/ttyUSB*`, 921600)** 를 PC에 꽂는다.
  모든 방의 패킷이 이 포트로 나온다. 보드의 다른 USB 포트(`/dev/ttyACM*`)는 부팅 로그만 나온다.
- 펌웨어는 `origin/csi-tx` 브랜치(`csi-tx/`, `csi-rx/`, `csi-main/`). 보드에는 이미 올라가 있다. 다시 올리려면
  그 브랜치에서 ESP-IDF v6.0.3으로 `idf.py -p <포트> flash` (방 번호는 `-DCSI_LINK_ID=<n>`).

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
