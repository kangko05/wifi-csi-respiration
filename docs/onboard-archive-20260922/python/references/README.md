# 참고 자료

자료는 원래 위치에서 읽기 전용으로 참조한다. 새 프로젝트 초기화 과정에서 논문·코드·원본 데이터는 복사하지 않았다.

## 레거시 프로젝트

경로: `../../wifi-csi-proto` (`python/` 폴더 기준), 절대 경로 `C:/Users/kang/workspace/wifi-csi-proto`. 레거시 비교 실행에 필요한 일부 코드는 `vendor/wifi-csi-proto/`에 바이트 동일 복사본으로 들어 있다(`VENDOR.json`).

우선 확인:

- `PROGRESS.md`: 사용자 결정, 현재까지 수행한 탐색과 실패, 측정 이력.
- `EXPERIMENT_CHECKLIST.md`: 기존25개 탐색 중단 조건, 완료·보류 실험.
- `references/ratnam_method_audit.md`: 정적 성분과 게인 보정의 구분, 원문 알고리즘과 백업 구현의 차이.
- `references/phase_cir_integration.md`: 위상/복소 CSI와 CIR 경로, 입력 매핑과 IFFT·confidence의 한계.
- `references/wicyclops_estimator.md`: 기존 추정기 설명.
- `README.md`, `main.py`, `src/csi_pipeline/`, `tests/`: 수집과 입력 해석·시간·마스크 계약의 재사용 검토.
- `outputs/`: 과거 비교 결과. 진단값과 채택, 실제 해상도와 고정 오차 허용폭을 구분한다.

`references/manual_breath_counts.csv`는 레거시 평가 전용이다. 새 데이터의 정답이나 전처리/선택 입력으로 사용하지 않는다. 레거시의 `CLAUDE.md`와 `.claude`는 읽거나 적용하지 않는다.

## 로컬 논문

경로: `~/Documents/csi-respiration` (2026-09-19 사용자 지정).

현재 위치에서 파일 존재를 확인한 주요 자료:

- `Optimal_Preprocessing_of_WiFi_CSI_for_Sensing_Applications.pdf`: Ratnam 등의 CSI 게인·위상 오차 보정.
- `ComplexBeat_Breathing_Rate_from_Complex_CSI.pdf`: 복소 CSI/CIR 기반 호흡 처리. 다중 안테나 등 논문 조건을 현재 하드웨어에 그대로 적용하지 않는다.
- `wi-cyclops.pdf`: 단일 안테나 기반 호흡 처리 참고.
- `preprocessing of wifi csi.pdf`: Ratnam 원문과 다른 논문이므로 파일명만으로 혼동하지 않는다.
- `sensors-21-03505.pdf`: 내용·서지 확인 후 사용한다.

논문은 로컬 원문부터 확인한다. 근거에는 파일명·서지·절/수식/페이지와 적용 가정을 기록한다. 웹에서 확인한 추가 근거는 원문 출처를 남긴다. 논문 결과를 현재 ESP32의 검증 결과로 표현하지 않는다.

## 코드 재사용 기록

필요한 부분만 승인된 범위에서 가져온다. 원본 경로와 버전 또는 SHA-256, 가져온 날짜, 원본 대비 변경점, 확인한 입력/출력 계약과 테스트를 기록한다. 외부 논문·레거시 경로를 런타임 의존성으로 묶지 않고, 필요 시 새 프로젝트에 보존한 사본을 사용한다. 참조 원본은 수정하지 않는다.

## 수동 기준 (이 프로젝트 데이터)

- `manual_breath_counts_20260916.csv` (+ `.provenance.json`): 2026-09-16 수집 21개 중 19개의 사용자 보고 호흡 횟수다. 추정이 끝난 뒤 제공됐다. 명목 120초 구간으로 해석했고 동기화는 미확인이다. 마지막 빈 방 2개는 미제공이다. 평가에만 쓰고 추정 입력으로 쓰지 않는다.
