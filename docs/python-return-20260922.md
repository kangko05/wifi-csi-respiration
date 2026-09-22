# Python 루트 복원 및 온보드 복구 방법 — 2026-09-22

## 완료 상태

사용자 요청으로 현재 작업을 보존한 뒤, Python 파일을 하위 폴더로 옮기기 전
커밋 `4a0421d`에서 `python-resume` 브랜치를 만들었다. 당시 Git에 없던 Python
코드·전체 문서·참고 자료·출력은 백업에서 가져왔다. 기존 데이터는 그대로 유지했다.

- 온보드 보존 브랜치: `archive/onboard-20260922`, 원격 저장 완료, 커밋 `93dd01a`.
  최적화 코드·테스트·전체 문서·참고 자료·결과 문서를 포함한다.
- 현재 Python 프로젝트: 루트의 `collect_legacy.py`, `src/`, `scripts/`, `tests/`,
  `vendor/`, `references/`, `docs/`, `.venv/`. 데이터는 `data/`, 결과는 `outputs/`.
- 원래 `python/outputs/`는 루트 `outputs/`에 합쳤으며 기존 출력과 충돌하지 않았다.
- 기존 루트와 Python의 문서 전체는 `docs/onboard-archive-20260922/`에 원래 상대
  경로 그대로도 보존했다. 문서·참고자료 50개를 SHA256으로 대조했다.
  현재 사용하는 지침·안내 문서는 복원된 경로에 맞게 갱신했다.
- 루트 `AGENTS.md`는 Python 지침을 사용한다. 이전 C용 루트 지침은
  `docs/onboard-archive-20260922/AGENTS.md`에 있다.
- 30/20초 온보드 윈도우 실험·추가 알고리즘 변경·플래시는 하지 않았다.
  디스크의 RX/TX 소스는 `4a0421d` 시점이다. 실제 RX 보드는 이전 일반 float
  펌웨어 그대로이므로 소스와 보드 펌웨어 시점이 다르다.

## 백업 위치

`outputs/backups/onboard-20260922-before-python/`:

- `workspace.tar.gz` / `workspace.sha256`: 전환 전 전체 작업 파일. 개인 데이터,
  실험 산출물, 가상환경, 빌드 파일도 포함한다. `.git`은 별도 bundle로 보존한다.
- `repository-all-documents.bundle`: 문서 전체를 포함한 보존 브랜치까지의 Git 이력.
- `all-documents.tar.gz` / `all-documents-manifest.json`: 전체 문서 원본과 해시.
- `preserved-hashes.json`: 원본 데이터와 vendor 대조용 해시.
- `old-local-paths/`: 이전 빌드 캐시·남은 Python 폴더 등. 새 루트에서 낡은 C 빌드
  캐시를 사용하지 않도록 보관 위치만 옮겼다.

전체 로컬 백업은 약 433 MiB이며 GitHub에는 올리지 않았다. GitHub에는 코드와
문서·참고자료·결과 문서가 있고, 원시 캡처·대형 산출물·환경은 로컬 백업에 있다.
다른 PC에서 원본 데이터까지 복원하려면 이 백업도 별도로 복사해야 한다.

## 온보드 코드로 돌아가기

작업 중인 변경을 먼저 커밋한 다음:

```bash
git switch archive/onboard-20260922
# 다시 Python 작업으로 복귀
git switch python-resume
```

동시에 두 프로젝트를 두려면 별도 폴더에 worktree를 만든다:

```bash
git worktree add ../wifi-csi-respiration-onboard archive/onboard-20260922
```

브랜치 전환은 코드·추적 문서를 복원한다. 가상환경·데이터·빌드는 브랜치 전환으로
자동 이동하지 않는다. 전체 로컬 백업을 복원할 때는 현재 폴더에 덮어풀지 말고
새 빈 폴더에 먼저 풀어 필요한 파일을 가져온다. 이전 경로가 박힌 CMake 빌드는
재사용하지 말고 새로 구성한다. 원격 없이 Git 이력을 복구할 때는 bundle에서 clone한다.

## 검증

코드 변경은 데이터 루트·모듈 경로·이전 `python/` 접두사 수정으로 제한했다.
C 실행 래퍼는 보존했지만 현재 브랜치에는 C 실행 파일이 없으므로 실제 C 계산에는
보존 브랜치의 별도 빌드가 필요하다. 계산 알고리즘과 기존 실패는 수정하지 않았다.

복원 전후 Python 테스트 각각 85개 실행, 동일한 입력 검증 실패 14건을 확인했다.
새 실패·에러는 없고 데이터 테스트도 건너뛰지 않았다. 로그는 백업 폴더의
`python-tests-before.log`, `python-tests-after.log`에 있다. `pip check`도 통과했다.
데이터와 vendor의 비캐시 파일 132개 해시가 복원 전과 같다.

```bash
.venv/bin/python -B -m unittest discover -s tests -v
.venv/bin/python -B collect_legacy.py --help
```

다음 작업은 Python 전처리의 기존 입력 검증 실패와 분석 기준선을 확인하는 것이다.
서버 장비·전송 방식은 미정이며 서버 연결은 시작하지 않았다.
