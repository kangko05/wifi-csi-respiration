# wifi-csi-respiration

ESP32-C5 TX/RX의 새 측정 데이터를 보며 호흡 관측과 추정 방식을 다시 검토하는 프로젝트다. 방 안에서 호흡을 관측하는 것이 장기 목표이며, 우선 멂(기존 기준90–100cm)의 약한 호흡 변화를 안정적으로 관측할 조건을 찾는다.

기존 `../../wifi-csi-proto`는 원본·구현·실험 이력을 보존하는 레거시 참고 자료로 둔다. 이 프로젝트의 메인 방식을 진폭·위상/복소 CSI·CIR 중 하나로 미리 고정하지 않는다.

## 현재 상태

- 원본 수집기(`collect_legacy.py`), 오프라인 테스트, [수집 문서](docs/data-collection.md)가 있다. 2026-09-16 옆 교수회의실에서 21개 세션(재실 거리군 30–500cm 각 3개, 빈 방 3개)을 수집했다(`../data/`).
- 2026-09-17: 21개 세션을 레거시 비교 경로에 1회 통과시켰다([기록](docs/legacy-run-20260917.md)). 빈 방에서도 phase·cir 후보가 채택됐으므로, 이 결과를 호흡 검출의 증거로 읽지 않는다.
- 2026-09-17 사용자 결정: 신호처리를 **RX ESP32-C5 온디바이스 C**로 포팅한다. Python 쪽은 지우지 않고 이 `python/` 폴더를 독립 Python 프로젝트 루트로 삼았다. 앞으로 수집과 C 결과 대조·검증에 쓴다. 저장소 루트(`..`)는 C/펌웨어 쪽이다. C 신호처리는 송수신 펌웨어(`../csi-rx/`, `../csi-tx/`)와 분리한 ESP-IDF 컴포넌트로 만드는 안을 검토 중이다(아직 생성하지 않음).
- 측정 장소는 옆 교수회의실로 확정했다.
- TX/RX는 같은 케이스, 새 케이스 장착 여부는 미확인이다.
- 사용자 지정 포트는 RX COM3, TX COM4다. 수집기는 지정한 포트 하나만 열고 TX 포트는 열지 않는다. 실제 연결은 수집 전에 확인한다.
- 실제 TX 출력값은 미확인이다. 출력 비교는 사용자 결정으로 미뤘다.
- 수집률·반복 횟수·거리 조건·출력 비교값은 아직 확정하지 않았다.

## 시작 순서

1. [AGENTS.md](AGENTS.md)의 목표·보존 계약·협업 지침을 읽는다.
2. [PROGRESS.md](PROGRESS.md)에서 마지막 결정과 미확인 조건을 확인한다.
3. 실제 장비/펌웨어/TX 출력과 배치를 확인하고 첫 측정 계획을 정한다.
4. 승인된 범위에서 필요한 최소 수집·입력 검증 기능을 Claude Code에 맡긴다. 검증된 레거시 부분의 재사용 여부도 이때 결정한다.
5. 새 원본을 보존하고 진폭·위상 등 관측 상태를 확인한 뒤 후속 처리를 선택한다.

코드·테스트·구현 문서화는 로컬 Claude Code CLI에 위임하고, Codex는 방향·범위·논문 해석·결과 검토를 맡는다. 작은 초기 지침 작성은 관리 작업으로 직접 수행했다. 측정 준비가 되었다는 뜻은 아니다.

## 자료와 파일 관리

- [참고 자료](references/README.md): 레거시와 로컬 논문 위치, 우선 읽을 문서.
- `../data/`: 수집 원본. C와 같이 쓰므로 **저장소 루트**에 둔다. 세션별 새 ID를 쓰며 덮어쓰지 않는다. 스크립트는 `DATA_ROOT = ROOT.parent / "data"`로, 수집기는 `--data-root` 기본값으로 이 위치를 가리킨다.
- `outputs/`: 파생 분석/보고서. 원본과 분리한다.
- `src/`, `tests/`, `scripts/`, `collect_legacy.py`: 수집기·어댑터·전처리 실험·테스트·스크립트.
- `../csi-rx/`, `../csi-tx/`: ESP-IDF 펌웨어(git 추적).
- 데이터·출력·가상환경은 `.gitignore`에 포함했다. Git 제외는 백업이 아니므로 원본 보존/백업은 별도로 관리한다.
- `vendor/wifi-csi-proto/`: 레거시 비교에 필요한 레거시 코드의 바이트 동일 복사본(커밋 `397813b9…`, 파일 해시는 `VENDOR.json`). 레거시 비교와 레거시 리더 테스트는 옆 폴더 체크아웃이나 레거시 `.venv` 없이 이 복사본을 쓴다. 복사본은 수정하지 않는다.
- 저장소 루트(`..`)가 Git 저장소다(`main`, 작업 브랜치 `preprocessing`). 코드(`src/`, `tests/`, `scripts/`, `vendor/`, `collect_legacy.py`, `requirements.txt`)는 git으로 추적하므로 클론만으로 실행할 수 있다. 문서(`AGENTS.md`, `PROJECT.md`, `PROGRESS.md`, `docs/`)와 `references/`는 `.git/info/exclude`로, `data/`·`outputs/`는 `.gitignore`로 추적에서 빠져 있다. 백업이 아니므로 별도로 보존한다.

## 실행 (이 `python/` 폴더 기준)

```powershell
C:/Users/kang/anaconda3/python.exe -m venv .venv
.venv/Scripts/python.exe -m pip install --no-cache-dir -r requirements.txt   # pyserial, numpy, scipy, matplotlib, threadpoolctl
.venv/Scripts/python.exe -B -m unittest discover -s tests
.venv/Scripts/python.exe -B collect_legacy.py --help
```

수집기 사용법·출력 형식·보존 계약은 [docs/data-collection.md](docs/data-collection.md)에 있다.
2026-09-17 `python/` 이동과 `.venv` 재생성 후 위 명령을 실행했다. 테스트는 85개가 돌고 14개가 실패한다. 실패는 모두 작성 중인 `test_preprocessing_to_csidata`에서 나오며, 이동 전에도 같았다. `data/`가 없는 클론에서는 실데이터 테스트 1개가 skip된다.
레거시 비교 재실행 명령은 [docs/legacy-run-20260917.md](docs/legacy-run-20260917.md) 1.2절에 있다(이 `.venv` 하나로 실행).
