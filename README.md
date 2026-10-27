# wifi-csi-respiration (demo-ui)

## 준비

```
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows
.venv/bin/pip install -r requirements.txt          # Linux
```

Python 3.10 이상. 아래 명령은 저장소 루트에서 실행한다. Windows에서는 `python -X utf8`로 실행한다.

## 실행

```
python tools/demo_ui.py demo --serial <메인 ESP USB serial> --start-delay 60 --port 8080
```

같은 네트워크의 폰이나 노트북에서 `http://<이 PC IP>:8080/`을 연다.

1. **calibrate** 누르고 방을 비운다. 60초 뒤 3분 동안 기준선을 잰다. 인형은 꺼 둔다. 기준선은 `baselines/1_2.json`에 저장된다.
2. **start** 누르고 인형을 켠다. 페이지에 준비 중 / 확인 중 / 호흡 있음 / 무호흡이 뜬다.
3. **stop** 으로 끝낸다.

- 기준선 측정이 끝나면 터미널에 `threshold=...`가 찍힌다. 중앙값이 3을 넘으면 방이 조용하지 않았던 것이니 다시 잰다.
- 메인 ESP가 UDP로 받는 구성이면 `--serial` 대신 `--udp-port 3334`.
- 보드 없이 페이지만 돌려 보려면 `--fake`.
- 마커(인형 켬·멈춤·재개)를 남기려면 `python tools/marker_server.py recordings/<세션> --port 8090 --no-apnea-hold`를 따로 띄운다.

보드 설정(채널, 역할)은 `docs/20261026-demo-run.md` 구성 표를 따른다. 10-26 기준 메인 ESP는 ESP-NOW 수신, 케이스 TX/RX는 ch36 bw20, RX `relay=2`.

## 녹화 다시 판정

```
python -m breathing replay recordings/<세션> --source ari2 --room 1 --node 2 --baseline-file baselines/1_2.json
python -m breathing check  recordings/<세션>                                   # 마커 기준 PASS/FAIL
python -m breathing baseline recordings/<세션> --source ari2 --baseline HH:MM:SS-HH:MM:SS --out baselines/x.json
```

csi-link 녹화(`frames.bin.gz`)는 `--source frames`. 밴드 기본값은 인형용 10–20/분이고 `--band 8 24`처럼 바꿀 수 있다.

## 테스트

```
pip install pytest
python -m pytest -q
```

리플레이 테스트는 `recordings/meeting_20261016_102602/`가 있을 때 돈다.

## 판정 규칙 요약

- 60초 창마다 호흡 대역 스펙트럼 점수를 기준선 문턱과 비교한다. 선이 2창 연속이면 호흡 있음.
- 호흡 중 15초 동안 호흡 봉우리가 없으면 무호흡. 선이 9창(90초) 연속 없어도 무호흡.
