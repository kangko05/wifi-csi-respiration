# 원본 CSI 수집기 (collect_legacy.py)

최종 갱신: 2026-09-17 (경로만 갱신. 2026-09-17 이 Python 프로젝트가 저장소의 `python/` 폴더로 옮겨졌다. 명령은 `python/` 폴더 기준이고, 원본은 저장소 루트 `../data/`에 저장된다. 진입 파일 이름은 `collect.py`에서 `collect_legacy.py`로 바뀌었다. `--help` 출력의 `usage: collect.py`는 argparse prog 이름이다.)

RX 콘솔이 내보내는 바이트를 **그대로** 저장하는 최소 수집기다. 신호처리·IQ 해석·서브캐리어
선택·호흡 추정은 이 도구의 범위가 아니다. 사용자는 옆 교수회의실 수집을 요청했고, 실제 실행은
남은 조건(수집 길이·재실·거리·자세·현장 준비)이 확정된 뒤에 한다. 이 문서 작성 시점까지
**장비를 연결한 실행은 없다.** 환경 구성과 오프라인 테스트는 2026-09-16에 검증했다(2·8절).

## 1. 범위

| 한다 | 하지 않는다 |
| --- | --- |
| 지정한 포트 1개를 열고 수신 바이트 전부를 `serial.bin`에 append | 포트 자동 탐색으로 TX/RX 역할 결정 |
| 청크·물리 라인 단위 인덱스(원본 바이트 오프셋/길이)와 호스트 수신 시각 기록 | 장치로 쓰기, 리셋 펄스, 입력 버퍼 비우기 |
| 와이어 메타데이터 문자열 원문 보존, 파싱 성공/오류 기록 | base64 payload를 별도 배열로 중복 저장 |
| 상태(`recording`/`complete`/`interrupted`/`error`)와 카운터, 원본 SHA256 기록 | 0 채움·보간·타임스탬프 보정·중복 제거 |
| 조건(장소/재실/자세/거리/메모)을 입력받은 대로 기록, 미입력은 unknown(null) | 미확인 조건 추정, 메타데이터를 처리 정답으로 사용 |

`--duration`은 유한한 양수여야 하며, 기본 보율은 RX 콘솔 설정과 같은 **921600**이다
(`csi-rx/sdkconfig`의 `CONFIG_ESP_CONSOLE_UART_BAUDRATE=921600`). TX(COM4)는 열지 않는다.

## 2. 설치 (2026-09-16 이 환경에서 실행·검증됨)

```powershell
# 작업 폴더: C:/Users/kang/workspace/wifi-csi-respiration/python
C:/Users/kang/anaconda3/python.exe -m venv .venv
.venv/Scripts/python.exe -m pip install --no-cache-dir pyserial==3.5
```

`pyserial 3.5` 설치와 `import serial`(및 `serial.tools.list_ports`)까지 확인했다. 장치를 연
실행은 아니다.

- 전역 파이썬에 설치하지 않는다. 설치 실패(네트워크·인증 등)는 그대로 보고하고 전역 설치로
  대체하지 않는다.
- 테스트는 표준 라이브러리 `unittest`만 쓰므로 `.venv` 없이도 돌릴 수 있다. 수집 실행에는
  `pyserial`이 필요하며, 없으면 수집기가 설치 방법을 담은 오류를 내고 종료한다(포트는 열지 않는다).

## 3. 사용법

```powershell
.venv/Scripts/python.exe -B collect_legacy.py --help          # 2026-09-16 실행 확인(포트를 열지 않음)
.venv/Scripts/python.exe collect_legacy.py --list-ports       # 미실행: 포트 목록만 출력하나 아직 돌리지 않았다
.venv/Scripts/python.exe collect_legacy.py --port COM3 --duration <초> --location "옆 교수회의실"  # 미실행
```

| 옵션 | 설명 |
| --- | --- |
| `--port` | 필수. 열 포트 하나(RX는 사용자 지정 COM3). 역할 자동 판정 없음 |
| `--duration` | 필수. 유한한 양수 초 |
| `--location` | 필수. 측정 장소 문자열을 그대로 기록 |
| `--baud` | 기본 921600 |
| `--idle-timeout` | 기본 10초. 이 시간 동안 **바이트가 하나도** 안 들어오면 실패로 끝내고 파일은 남긴다 |
| `--data-root` | 기본 `<저장소 루트>/data` (`python/`에서 보면 `../data/`) |
| `--occupancy` / `--posture` / `--distance-cm` / `--notes` | 선택. 미지정은 unknown(null), `--notes ""`는 "의도적으로 비움" |
| `--list-ports` | 포트 목록만 출력하고 종료. 포트를 열지 않는다 |

종료 코드: `0` 정상 완료, `130` Ctrl+C 중단(부분 세션 보존), `1` 오류(포트 열기 실패·idle
timeout·연결 끊김·메타데이터 기록 실패). 메타데이터를 끝까지 쓰지 못하면 성공이라고 말하지
않고 0이 아닌 코드로 끝난다.

실행 중에는 시작/종료 시각(UTC + 호스트 로컬)과 약 10초 간격 진행 상황을 출력한다. 카운트다운
기능은 넣지 않았다(측정 조건이 정해진 뒤 필요하면 추가).

## 4. 출력 파일 (`data/<session_id>/`)

세션 ID는 `YYYYMMDDTHHMMSS_microseconds_random`이고 `mkdir(exist_ok=False)`로 만든다. 같은
이름이 이미 있으면 **아무것도 건드리지 않고** 실패한다. 닫은 뒤 원본은 불변으로 취급한다.

1. `serial.bin` — `read()`가 돌려준 바이트 전부(부팅 로그, 헤더 줄, 깨진 줄, 끝의 미완성 줄 포함)를
   수정 없이 append. 수집기는 입력 버퍼를 비우지 않는다(`reset_input_buffer()` 호출 없음).
2. `chunks.csv` — `chunk_index, byte_offset, byte_length, host_elapsed_s, host_utc`.
   읽기 1회 = 1행. 오프셋을 이어 붙이면 `serial.bin`이 그대로 재구성된다.
3. `lines.csv` — 물리 라인 1개 = 1행.
   - 위치: `line_index, byte_offset, byte_length(종결자 제외), terminator(crlf/lf/none), terminator_length`
   - 판정: `kind(csi/header/other/oversized/trailing_partial), parse_ok, error`
   - 호스트 수신 시각: `arrival_chunk_index, host_elapsed_s, host_utc` (장치 `local_timestamp`와 별개)
   - payload 위치: `data_byte_offset, data_byte_length`(`serial.bin` 절대 오프셋), `decoded_length`, `len_match`
   - 와이어 메타데이터 원문: `seq, rssi, noise_floor, fft_gain, agc_gain, channel, local_timestamp,
     sig_len, rx_format, len, first_word, compensate_gain, dropped` (문자열 그대로, 값 수정 없음)
   - `trailing_partial` 행을 포함한 **모든 행**에 대해
     `sum(byte_length + terminator_length) == raw_bytes`가 성립한다. `trailing_partial_bytes`는
     이미 이 합에 포함된 바이트를 따로 세어 둔 참고용 카운터이며 다시 더하지 않는다.
4. `session.json` — 상태/설정/조건/미확인 목록/시작·종료 UTC/카운터/payload 길이 분포/`raw_sha256`/
   오류 종류/trailing partial 바이트 수. 캡처 **시도 전에** `status: recording`으로 먼저 쓰고,
   끝날 때 `complete`/`interrupted`/`error`로 덮어쓴다. 프로세스가 강제 종료되면 `recording`이
   남는데, 이는 "정상 종료되지 않은 부분 세션"이라는 정직한 복구 표시다.

디코딩된 payload는 따로 저장하지 않는다. `serial.bin`의 `data_byte_offset/length` 구간을
base64 디코딩하면 원본 바이트를 그대로 복원할 수 있다.

## 5. 보존 계약과 한계

지키는 것:

- `read()`가 돌려준 바이트는 변형·필터링 없이 저장된다. 로그·깨진 줄·미완성 줄도 남는다.
- 끝의 미완성 줄(`trailing_partial`) 행의 `host_elapsed_s`/`host_utc`는 그 바이트가 실제로 도착한
  마지막 청크(`arrival_chunk_index`)의 수신 시각이다. 캡처가 끝난 시각은 `session.json`의
  `timing.elapsed_host_s`에 따로 남는다.
- 시간 간격을 압축하지 않는다. 긴 공백은 `chunks.csv`/`lines.csv`의 시간 차이에 그대로 보인다.
- 호스트 수신 시각(monotonic elapsed + UTC)과 장치 `local_timestamp`를 섞지 않는다.
- 길이 불일치·base64 오류·필드 수 오류는 **행으로 남기고** 버리지 않는다. CSI 길이를 234바이트로
  가정하지 않는다(펌웨어 버퍼 상한은 512바이트이며 `len`은 행마다 기록된다).
- `parse_ok`는 **문법**만 뜻한다. `first_word`(펌웨어 `first_word_invalid`)는 플래그로 보존하며,
  문법이 맞는다고 그 CSI 샘플이 유효하다고 표시하지 않는다. `compensate_gain`이 `nan`/`inf`/`-inf`면
  `nonfinite_compensate_gain` 파싱 오류로 남긴다(원문 문자열은 그대로 보존하고 0으로 채우거나
  CSI를 마스킹하지 않는다).
- 파일 생성·기록·flush·해시·메타데이터 기록 중 어떤 IO 실패가 나도 `complete`라고 말하지 않는다.
  받은 바이트는 디스크에 남은 만큼 보존하고, 가능한 범위에서 `error`/`error_type: io_error`를
  기록한 뒤 0이 아닌 코드로 끝난다. 해시를 못 구하면 `raw_sha256: null`과 `raw_sha256_error`를
  남긴다. `session.json` 자체를 끝까지 못 쓰면 상태는 `recording`(미완결)으로 남고 CLI가 그 사실을
  stderr로 알린다. 시리얼 포트는 모든 경로에서 닫는다.
- 바이트 순서는 수신 순서 그대로 유지한다. signed/IQ 해석과 서브캐리어 선택은 **미정·연기**다.
- 메모리는 제한된다. 원본은 증분 기록 후 약 1초 간격으로 flush하고, 64KiB를 넘는 줄은 버퍼링을
  멈추되(=`oversized`로 표시) 원본 바이트와 오프셋은 그대로 보존한다.

한계(약속하지 않는 것):

- 포트를 열 때 `Serial(port=None)` 후 `dtr=False, rts=False`를 먼저 설정하지만, **OS 드라이버가
  핸들 오픈 시 제어선을 건드릴 수 있어 보드 리셋이 없다고 보장하지 않는다.** 열린 직후의 부팅
  로그나 과도 구간도 원본에 그대로 남으므로, 필요하면 나중에 오프셋으로 구간을 지정해 제외한다.
- 보존 보장은 **`read()`가 돌려준 바이트**에 대한 것이다. 수집기는 애플리케이션 차원의 입력 flush를
  하지 않지만, 열기 전/열기 시점에 큐에 있던 바이트가 드라이버·오픈 과정의 버퍼링으로 첫 read 전에
  버려질 수 있다. "열 때 큐에 있던 바이트가 반드시 보존된다"고 약속하지 않는다.
- 수집기는 호스트가 받은 것만 기록한다. 보드 동일성, 실제 동작 TX 출력, 실행 중인 펌웨어 버전,
  채널/대역폭/수집률은 **검증하지 않는다**(`session.json`의 `verification`에 false로 명시).
- `dropped`는 보드 큐 오버플로 계수이며 무선 수신률이 아니다.
- idle timeout은 "바이트" 기준이다. 로그만 나오고 CSI가 없으면 오류가 아니라 `csi_lines: 0`인
  정상 종료로 남는다.
- 조건(`conditions`) 값은 운영자가 적은 맥락일 뿐이며 어떤 처리에도 정답으로 쓰지 않는다.

## 6. 원본과 파생

- 원본: `data/<session_id>/` 아래 4개 파일. 닫은 뒤 수정·삭제·덮어쓰기 금지.
- 파생: 이후 분석/보고는 `outputs/` 아래에 새로 만들고 원본 세션 ID를 참조한다.
- 수동 호흡 횟수·기준 라벨은 **수집 후 별도 평가 기록**(예: `outputs/<session_id>/evaluation.json`)에
  남긴다. 수집기는 라벨을 받지 않고 `session.json`의 `labels.manual_reference_recorded`는 항상
  false다. 라벨은 추정 후 평가에만 쓰고 열·주파수·후보 선택에 쓰지 않는다.

## 7. 기록 예시 (실측 아님, 자리표시자 포함)

실행 전 계획과 실행 후 기록을 같은 형식으로 남긴다. 아래 값 중 `<...>`는 아직 정해지지 않았다.

```text
장소:        옆 교수회의실 (사용자 지정)
포트/보율:   COM3 (RX) / 921600, TX(COM4)는 열지 않음
수집 길이:   <미정: --duration 초>
재실/자세:   <미정: 사람 수·자세>
거리:        <미정: TX/RX–대상 거리 cm>
장비 조건:   케이스 장착 여부 <미확인>, 실제 TX 출력 <미확인>, 펌웨어 버전 <미확인>
명령:        .venv/Scripts/python.exe collect_legacy.py --port COM3 --duration <초> --location "옆 교수회의실"
세션 ID:     <실행 후 출력된 값>
결과:        status <complete/interrupted/error>, raw_bytes <..>, csi_lines <..>,
             parse errors <..>, raw_sha256 <..>
비고:        <중간 이동·문 열림 등 관찰 사항>
```

측정 계획(길이·반복·거리·자세)은 이 문서가 정하지 않는다. 사용자가 조건을 확정하기 전에는
`<미정>`으로 남긴다.

## 8. 테스트

```powershell
.venv/Scripts/python.exe -B -m unittest discover -s tests -v
```

2026-09-16 실행 결과: **41개 테스트 전부 통과(실패 0).** (2026-09-17 `python/` 이동 후에는 어댑터·전처리 테스트까지 포함해 85개가 돌고, 작성 중인 `test_preprocessing_to_csidata`에서 14개가 실패한다.) 각 테스트 모듈이 스스로 `src/`를
`sys.path`에 넣으므로 설치 없이 위 명령 그대로 돌아간다(`tests/`가 discovery 루트이므로 `-t .`는
쓰지 않는다). 임시 파일은 `.tmp-tests/` 아래 `tempfile.TemporaryDirectory`로 만들고
테스트가 스스로 지운다.

`tests/`는 표준 라이브러리 `unittest`와 가짜 시리얼/시계만 사용하며 **포트를 열지 않는다**.
검증 대상: 청크 분할과 원본 바이트/오프셋 보존, 줄 정렬과 종결자, malformed/base64/길이 불일치,
`compensate_gain`의 nan/inf 거부와 유한값 통과, 가변 길이 payload, signed 경계 바이트
(0x80/0x7F/0xFF) 왕복, `first_word`·장치 타임스탬프 문자열 보존, 로그·oversized·trailing partial
정렬과 회계(끝 미완성 줄의 시각이 도착 청크와 일치), 시간 공백 비압축, 중복 세션 거부 시 기존
바이트 무변경, idle/무패킷, 중단·연결 끊김·포트 열기 실패 상태와 원본 보존, 주입한 디스크 실패
(원본 기록 실패·인덱스 파일 생성 실패·최종 메타데이터 기록 실패)에서의 비정상 종료와 바이트 보존,
시리얼 설정 호출 순서(mock), 인자 검증.

> 합성 테스트는 계산·보존 계약만 검증하며 실제 호흡 관측 정확도나 하드웨어 동작을 입증하지 않는다.
> 장비를 연결한 실행은 아직 없다.

## 9. 레거시 참고 기록

- 출처: `../../wifi-csi-proto/src/csi_pipeline/capture.py` (읽기 전용 참조)
  - HEAD `397813b960b8fd114ba5a78c367d5e26323613df`
  - SHA256 `EED00D6A3EBFCF9B5008D0192A8D6EA70269E2F644AEDA456C7448A8661A68B1`
- 가져온 아이디어: 원본 로그 우선 기록, `CSI_DATA` 라인 파싱과 와이어 필드 목록, idle timeout으로
  실패 처리, 중단 시 부분 세션 보존, 주기적 flush, 상태/카운터를 `session.json`에 기록.
- 바꾼 점: 고정 234바이트 numpy 경로 제거(길이를 가정하지 않음), payload 리스트·`csi_raw.npy`
  중복 저장 제거(오프셋으로 복원), 모든 물리 라인 인덱싱(버리는 줄 없음), 청크 단위 수신 시각
  기록, oversized/trailing partial 회계, 원본 SHA256, 조건 unknown 구분, 입력 검증·라벨 저장
  기능 분리(수집기는 라벨을 받지 않음).
- 레거시 프로젝트를 복사하지 않았고, 레거시 데이터나 지시 문서를 근거로 사용하지 않았다.
