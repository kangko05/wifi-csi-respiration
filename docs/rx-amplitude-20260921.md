# RX 진폭 분석 연결 및 기존 경로 대조

> 후속 실측: [단계별 최적화 비교](rx-amplitude-optimization-20260921.md).
> 일반 RX는 검증 후 FFT/FIR 단정밀도만 켰으며, 나머지 변경은 실험 옵션으로 유지한다.

## FFT 회전 계수 캐시 추가

RX 시작 시 4,096점 FFT의 반 주기 회전 계수를 `double` 실수부·허수부로
한 번 계산한다. PSRAM에 **32,768바이트(32KiB)**를 추가로 할당하고,
2,048점 ACF 순·역 FFT에서도 인덱스 간격을 바꿔 같은 표를 재사용한다.
분석 중 반복되던 `sin/cos` 계산을 테이블 조회로 바꿨으며, butterfly의
곱셈·덧셈과 float 저장, 역 FFT 정규화는 기존 순서를 유지했다.

`csi_fft_plan_init()`은 호출자가 제공한 공간에 계수를 준비하고,
`csi_fft_planned()`/`csi_ifft_planned()`는 이를 읽기만 한다.
raw 분석 입력의 `fft_plan`은 선택 사항이다. 기존 프레임 기반 경로와
위상 FFT는 캐시를 사용하지 않아 비교 기준을 유지한다. 캐시 용량을
넘는 FFT는 오류를 반환하므로 윈도우·분석 설정을 바꾸면 용량도 확인해야 한다.

```text
FFT cache ready: max_n=4096 bytes=32768 setup=...us
```

분석 관련 직접 할당량의 기존 약 103KiB에 캐시 32KiB가 추가돼 약
135KiB가 된다. 캐시는 윈도우마다 재할당하지 않는다. 다른 태스크나
라이브러리의 내부 메모리는 별도다. 이 변경의 보드 속도 개선 폭은
재플래시 후 `amp timing`의 PSD·ACF 시간과 전체 시간을 비교해야 한다.

캐시 적용 후 전체 테스트 **21개 통과**, RX 빌드 통과. 순·역 FFT의 여러
길이와 실측 21개 세션의 전체·60초 구간에서 캐시 미사용 기존 결과와
바이트 단위 일치를 확인했다. 할당 실패 정리와 캐시 용량·인자 검사도
통과했다. 검증 로그는 `outputs/fft-cache-validation/`에 보관했다.

## 후속 보드 측정과 단계별 계측

최초 보드 실행에서 5,992개 레코드·117 bins를 처리해 `withheld`, bin 35,
PSD 3.076 BPM, ACF 47.307 BPM, reasons `0xb8`이 출력됐다.
전체 시간은 **81,745,118us (약 81.75초)**, 종료 시 큐는 200개로 가득 찼다.
분석 전후 힙 할당량은 복귀했지만, 이 처리 속도로 연속 수집을 유지할 수는 없다.
완료 직후 `queue_drops=0`은 이전 레코드에 담긴 값이므로 무손실의 증거가 아니다.

현재 실행은 부팅당 한 번 분석하는 진단 모드이며 수집은 이후에도 계속한다.
이전 아래 설명의 반복 분석 주기는 현재 진단 모드에는 적용되지 않는다.
출력은 `csi_output.c/.h`에 분리돼 있고, 처리 태스크는 구조체를 전달한다.
비동기 외부 송신으로 교체할 때는 전달된 구조체를 송신 측 큐에 복사해야 한다.

병목 확인을 위해 raw 분석 경로에 선택적 단계 진입 콜백을 추가했다.
펌웨어의 `esp_timer_get_time()`으로 각 단계의 경과 시간을 누적한다.
로그는 출력 모듈에서만 생성하며 분석식·판정 기준은 변경하지 않았다.

```text
amp progress stage=fir bin=1/117 elapsed=...ms
amp progress stage=acf bin=.../117 elapsed=...ms
amp timing stage=validate total=...us
amp timing stage=eligibility total=...us
amp timing stage=setup total=...us
amp timing stage=interpolate total=...us
amp timing stage=fir total=...us
amp timing stage=detrend total=...us
amp timing stage=psd total=...us
amp timing stage=acf total=...us
amp timing stage=score total=...us
amp timing stage=yield total=...us
amp timing stage=cleanup total=...us
amp timing wall=...us (stage totals exclude output)
```

각 단계를 처음 진입할 때 한 번, 이후에는 단계 경계에서 약 2초 간격으로
진행 로그를 출력한다. 한 단계 자체가 오래 걸리면 그 단계 중간에 별도
타이머 로그를 발생시키지는 않는다. 진행 번호는 1부터, 결과 bin 인덱스는 0부터다.
단계 합계는 진행 로그 출력 시간을 제외하지만 다른 태스크의 선점 시간까지
제외한 순수 CPU 시간은 아니다. `yield`는 명시적으로 태스크를 양보한 구간,
`wall`은 로그를 포함한 전체 분석 경과 시간이다. 새 계측의 실제 보드 수치는
재플래시 후 확인해야 하며, 아직 병목 원인을 확정하지 않았다.

2026-09-21. 기존 수집 구조는 [수신·윈도우 문서](rx-capture-window-20260921.md)를 참고한다.

## 변경 내용

`csi_amp_process_raw()`를 추가했다. 시간순 레코드를 반환하는 reader로 RX 링 윈도우를 직접 읽는다. 전체 `csi_frame_t` 배열이나 전체 서브캐리어의 보간 행렬을 생성하지 않고, 한 서브캐리어씩 처리하며 임시 배열을 재사용한다. 기존 `csi_amp_process()` 구현은 변경하지 않았다.

처리 순서:

1. 레코드 길이·타임스탬프·게인 값 유효성 검사.
2. 기존과 같은 유효 bin 판정 및 모든 유효 bin에 공통인 보간 시작·끝 계산.
3. 원시 I/Q에서 한 bin의 진폭을 계산하고 100Hz 격자로 선형 보간.
4. 기존 FIR의 계수 순서, double 누적, float 변환을 유지하며 다운샘플링 후 남을 출력만 계산.
5. 기존 함수로 추세 제거, Hann·PSD, ACF, 점수·판정 계산.
6. 해당 bin 결과만 저장하고 다음 bin에서 작업 공간 재사용.

**게인 보상을 곱한다는 앞선 설명은 정정한다.** 현재 `csi_preprocess()`는 게인 계수를 메타데이터에만 남긴다. 새 경로도 동일하게 진폭에 곱하지 않는다. `first_word_invalid`와 bin 개수에 따른 마스킹, 보간 허용 간격·비율, down=1 처리, 후보 선택 및 동점 처리도 유지했다.

## 메모리

60초를 양 끝 포함 6,001개 샘플로 표현하고, 117 bins, 기본 FIR 201 taps·10Hz 출력·FFT oversampling 4를 사용한 호스트 계측:

| 항목 | 바이트 |
|---|---:|
| 1개 bin 보간 배열 | 24,004 |
| 다운샘플 출력·추세 제거/Hann scratch | 4,648 |
| FFT 실수부·허수부, PSD 배열 | 49,160 |
| **새 함수의 최대 동시 임시 할당 합계** | **77,812** |
| 결과 구조체 (RX에서 PSRAM 할당) | 26,680 |
| FIR 계수 | 804 |
| **분석 관련 위 항목 합계** | **105,296 ≈ 102.83KiB** |

원시 윈도우 3,564,000B는 기존 할당을 재사용한다. 큐·태스크 스택·힙 관리 비용과 qsort 등 라이브러리 내부 작업 공간은 표에 포함하지 않는다. 계측은 새 경로에서 직접 호출한 malloc/free를 대상으로 한다. 샘플 수, 필터 설정, FFT 크기가 바뀌면 작업 공간도 바뀐다. 이는 요청한 할당 바이트 기준이며 보드에서 측정한 분석 중 최대 힙 사용량은 아니다.

임시 배열은 일반 malloc을 사용하므로 현재 `CONFIG_SPIRAM_USE_MALLOC=y` 설정에 따라 내부 RAM과 PSRAM에 분산될 수 있다. 결과 구조체와 원시 윈도우는 명시적으로 PSRAM에 둔다.

## 기존 결과와의 검증

저장된 실측 **21개 세션** 각각에 대해 전체 캡처와 처음·마지막 최대 6,000개 레코드 구간을 대조했다. 물리 배열의 시작 위치를 바꿔 링 버퍼 순회도 검사했다.

같은 레코드에 대해 기존 `csi_preprocess() → csi_amp_process()`와 새 raw 경로의 반환 상태가 일치했고, 성공한 호출은 **전체 결과 구조체가 바이트 단위로 일치**했다. 비교에는 모든 bin의 PSD/ACF 주파수·파워·점수·보류 사유, selected/best bin, 샘플 수·시간 정보가 포함된다. 일부 짧거나 gap이 큰 구간의 양쪽 process_error도 상태 일치로 확인한다.

추가 합성 검사는 시간 순환, jitter, 중복·역행 시각, first-word mask, 상수 신호, 짧은 입력, 긴 gap, 가변 게인 메타데이터, 비정렬 다운샘플 위치, down=1을 포함한다. 임시 할당 6곳을 각각 실패시켜 오류 반환과 누수 없는 해제도 확인했다.

```bash
python/.venv/bin/python -m unittest discover -s tests -v
```

결과: **20 tests passed**. 호스트 및 ESP32-C5 RX 빌드 통과. RX bin 크기는 `0xe04b0`, 현재 1MiB 앱 파티션에 약 12% 여유다. 로그와 요약은 `outputs/amplitude-raw-validation/` 아래 실행별 디렉터리에 저장했다.

테스트 파일은 `tests/test_amplitude_raw.py`, `tests/amplitude_raw_bridge.c`다. 위 수치 일치는 호스트에서 검증한 결과이며, 보드와 호스트 사이의 부동소수점 결과 일치까지 측정한 것은 아니다. 합성 후보 판정은 실제 호흡 정확도의 증거가 아니다.

## RX 동작과 다음 확인

처리 태스크가 거의 60초(59.9초 이상)의 지원 구간을 확보하고 큐의 밀린 데이터를 비운 뒤 분석한다. 분석 중에는 윈도우를 수정하지 않는다. 주기적으로 태스크를 양보해 idle 태스크가 실행되도록 하며, 그동안 수신 콜백은 계속 큐에 기록한다.

분석 완료 후 최소 1초를 기다렸다가 다음 분석을 시작한다. 정확히 1Hz로 결과를 보장하는 스케줄은 아니다. 출력 예시의 필드:

```text
amp candidate/withheld bin=... psd_bpm=... acf_bpm=... has_acf=...
    score=... reasons=0x... elapsed=...ms queued=...
```

`candidate`는 기존 주기성 조건을 통과했다는 의미이며 호흡 확정이 아니다. `withheld` 상태의 BPM은 진단용이다. 주기 후보 자체가 없으면 `withheld: no periodic candidate`, 입력·gap·설정·할당 문제는 `process_error`로 출력한다. 판정 비트는 `include/csi_resp/amplitude.h`의 `CSI_AMP_*`에 정의돼 있다.

**실제 보드에서 분석 시간과 큐 드롭은 아직 확인하지 않았다.** 계산 중 큐를 비우지 않으므로 약 2초 이상 소요되면 100Hz·200개 큐가 넘칠 수 있다. 1초 예산 초과 시 경고를 출력한다. 실시간성이 부족하면 계산 구조 또는 수집과 분석 사이의 버퍼 설계를 추가로 변경해야 한다. 이 작업에서는 플래시하지 않았다.
