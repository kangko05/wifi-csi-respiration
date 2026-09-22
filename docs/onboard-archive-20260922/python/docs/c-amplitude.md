# 기존 수집 데이터로 C 진폭 파이프라인 실행

저장소 루트 PowerShell에서:

```powershell
python/.venv/Scripts/python.exe python/scripts/run_c_amplitude.py
```

특정 세션만 실행:

```powershell
python/.venv/Scripts/python.exe python/scripts/run_c_amplitude.py --session 20260916T060200_034260_9dd9fd80
```

Python이 `serial.bin`의 CSI_DATA 줄을 읽어 base64를 signed `[im, re]` 바이트로
풀고, `build/csi_resp_main.exe --stdin`에 공백 구분 텍스트로 전달한다.
C `main`은 `csi_record_t`를 구성하여 `csi_preprocess`와 `csi_amp_process`를 호출한다.
Python에서는 신호처리를 하지 않는다. 실행 전에 CMake로 현재 코드를 빌드한다.
PC용 실행기이며 Python과 CMake/gcc가 필요하다.

각 세션 전체가 한 윈도우다. 기본 출력은 `outputs/c-amplitude/<UTC 실행 시각>/`이며
`--output <새 경로>`로 바꿀 수 있다. 기존 출력이나 원본 데이터 안에는 쓰지 않는다.

- `summary.csv`: 세션별 채택 후보와 진단용 PSD/ACF 결과를 구분한 요약.
- `<세션 ID>.json`: 서브캐리어별 결과, 보류 이유, 건너뛴 줄, 입력 정보와 원본 해시.
- `manifest.json`: 설정, 실행 파일과 소스 해시.

`weak_evidence`는 계산 실패가 아니라 채택 기준을 통과한 후보가 없다는 뜻이다.
`diagnostic_*`는 보류된 피크도 포함하므로 호흡 검출 결과로 간주하지 않는다.
수동 호흡수와 재실/거리 조건은 계산에 넣지 않는다. 원본 순서와 수신 시각을
보존하고 단일 uint32 wrap만 상대 시각으로 해석한다. 형식 변경/시각 역전은 거부한다.

현재 C 전처리는 raw I/Q를 사용하며 `compensate_gain`을 곱하지 않는다.
따라서 게인 보정 등을 포함한 과거 Python 전체 분석과 동일한 결과라고 보장하지 않는다.
주기 추정 기준을 바꾸려면 C 설정을 수정하고 다시 실행한다.
