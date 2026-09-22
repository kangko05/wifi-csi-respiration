# wifi-csi-respiration

ESP32-C5 TX/RX가 출력한 Wi-Fi CSI 원시 줄을 읽어 호흡수(bpm)를 추정하는 Python 도구다.

같은 입력에서 추정기 세 개를 독립적으로 실행한다. 추정기끼리 투표하지 않고, 전체를 합친 호흡수도 내지 않는다.

- `amplitude`: CSI 진폭의 PSD/ACF 주기성 추정 (기본 게인 보정 없음)
- `phase`: 복소 CSI 위상 변화로 추정
- `cir`: 복소 CSI를 지연 영역(CIR)으로 바꿔 추정 (`phase`와 한 번의 계산을 공유)

## 환경 준비

동작을 확인한 환경은 Python 3.14다. 최소 지원 버전은 따로 확인하지 않았다. 저장소 루트에서 실행한다.

```bash
python3 -m venv .venv

.venv/bin/pip install -r requirements.txt
```

## CLI 사용

```bash
.venv/bin/python main.py --help

# 기본: amplitude, phase, cir 셋 다 실행하고 JSON을 stdout으로 출력
.venv/bin/python main.py -i capture.csv

# 진폭만 실행
.venv/bin/python main.py -i capture.csv --methods amplitude

# 부트 로그 등 CSI가 아닌 줄이 섞인 파일
.venv/bin/python main.py -i boot_and_csi.txt --serial-log

# JSON을 새 파일에 저장
.venv/bin/python main.py -i capture.csv -o result.json
```

`capture.csv`는 사용자가 직접 수집한 파일을 가리키는 예시 이름이다. 저장소에 포함된 데이터가 아니다.

## 입력 형식

- RX 펌웨어가 출력한 완전한 `CSI_DATA` 줄만 받는다. 한 줄은 쉼표로 나뉜 필드 15개다.
  `CSI_DATA,seq,rssi,noise_floor,fft_gain,agc_gain,channel,local_timestamp,sig_len,rx_format,len,first_word,compensate_gain,dropped,<base64 CSI>`
- 지원 프로필: CSI 234바이트(복소 bin 117개), `rx_format=2`, 윈도우 전체가 단일 채널, `compensate_gain > 0`. 이 밖의 입력은 오류로 끝난다.
- 줄끝은 LF와 CRLF 모두 받는다. 빈 줄은 건너뛰고 개수를 센다.
- 기본 모드에서는 CSI가 아닌 줄이 하나라도 있으면 오류다. `--serial-log`를 주면 그런 줄을 건너뛰고 개수를 기록한다. 하지만 `CSI_DATA`가 들어 있는데 형식이 깨진 줄은 두 모드 모두 오류다. 이런 줄을 건너뛰는 옵션은 없다.

## 처리 방식

파일 하나 전체가 윈도우 하나다. EOF까지 읽은 뒤 한 번만 분석한다(`-i -`로 stdin을 줘도 같다). 실시간 시리얼 스트림 처리나 윈도우 분할, 반복 분석은 하지 않는다.

## 출력

- JSON은 stdout으로 나가고, `-o`를 주면 새 파일에만 쓴다. 이미 있는 파일은 덮어쓰지 않고 거부한다. 상위 디렉터리는 미리 있어야 하며 자동으로 만들지 않는다.
- 진단 메시지와 오류는 stderr로 나간다.
- 종료 코드: `0` 처리 완료(방법별 상태와 무관), `2` 사용법·설정 오류, `3` 입력 데이터 오류, `4` 파일 I/O 오류나 출력 거부.

`result.methods`에는 방법별 결과가 들어 있다.

| 필드            | 의미                                                                                                                                                      |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `status`        | `accepted`(판정 기준 통과), `withheld`(후보는 있으나 기준 미달), `unavailable`(데이터가 이 방법을 지원하지 않음), `insufficient_data`(패킷이나 길이 부족) |
| `accepted_bpm`  | 기준을 통과했을 때만 값이 있다. 그 외에는 `null`이다(0이 아니다)                                                                                          |
| `candidate_bpm` | 방법이 찾은 진단용 피크. `withheld`여도 표시된다. 없으면 `null`                                                                                           |
| `reasons`       | 상태를 정한 이유 목록                                                                                                                                     |
| `elapsed_s`     | 처리 시간(초). `phase`·`cir`는 공유 계산 시간이므로 두 값을 더하지 않는다                                                                                 |

`accepted_bpm`이 `null`이라고 호흡이 없다는 뜻은 아니다. 재실 여부나 무호흡도 판정하지 않는다.

## Python API (선택)

```bash
PYTHONPATH=src .venv/bin/python
```

```python
from csi_respiration import RespirationPipeline, PipelineConfig

lines = [...]  # 시간순으로 정렬한 완전한 "CSI_DATA,..." 줄. 줄끝(\r, \n)은 빼고 넣는다

result = RespirationPipeline(PipelineConfig(methods=("amplitude", "phase", "cir"))).process(lines)

for name, m in result.methods.items():
    print(name, m.status, m.accepted_bpm, m.candidate_bpm, m.reasons)
```

`process`는 원시 줄 대신 `preprocessing.CSIdata` 레코드 목록도 받는다. 줄끝이 남아 있는 줄은 오류로 거부된다.
