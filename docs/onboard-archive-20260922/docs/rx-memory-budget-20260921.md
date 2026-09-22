# RX 메모리 예산 확인 — 2026-09-21

## 결론과 확인 범위

현재 전체 윈도우 C 구현은 RX 내부 RAM에 그대로 넣을 수 없다. 100Hz·120초의 주요 동시 보유량은 진폭 약 37.29MiB, 위상 약 68.58MiB다. 사용자가 제공한 실행 보드의 초기 힙 영역은 총 273.125KiB이고, 이는 Wi-Fi 초기화·수집 시작 전 값이다. **수집 중 실제 free heap 및 largest free block은 아직 미측정**이다. 펌웨어·설정 변경, 포트 열기, 리셋, 플래시는 수행하지 않았다.

## 하드웨어 및 런타임 근거

- 로컬 ESP-IDF v6.0.3의 `components/soc/esp32c5/include/soc/soc.h`: 내부 D/IRAM 0x40800000–0x40860000 = 384KiB, LP RAM 16KiB. D/IRAM은 같은 물리 메모리를 공유하므로 두 번 더하지 않는다. 전체 SRAM 용량은 애플리케이션에 남는 힙이 아니다.
- 현재 `csi-rx/sdkconfig`와 생성된 설정에는 PSRAM 활성화가 없다(`# CONFIG_SPIRAM is not set`). `CONFIG_SOC_SPIRAM_SUPPORTED=y`는 칩의 지원 여부이며 탑재/활성화 증거가 아니다. 모듈 모델과 물리 PSRAM 용량은 미확인이다.
- 사용자가 제공한 보드 로그는 `csi-rx`, `e653ce5-dirty`, 2026-09-16 13:48:37, IDF v6.0.3, chip rev v1.2다. 현재 로컬 소스와 보드 바이너리가 동일하다고 간주하지 않는다.
- 초기 힙 영역: RAM 251,200B + RAM 12,120B + RTCRAM 16,360B = 279,680B (273.125KiB). 로그의 괄호 숫자는 정수 KiB 표기이므로 hex 길이로 계산했다. 여러 영역의 합은 단일 할당 가능 크기가 아니며 allocator overhead와 이후 할당도 고려해야 한다.
- 로그의 8192k는 감지된 **플래시** 용량이다. 이미지 헤더는 2048k이며, 둘 다 신호처리용 RAM 용량이 아니다. 제공된 구간에는 PSRAM 초기화 기록이 없지만 물리 부재까지 증명하지 않는다.

## RX 수집 코드에서 이미 사용하는 메모리

ESP32용 GCC 15.2.0, RV32 ILP32로 구조체 크기를 컴파일 확인했다. 결과는 `outputs/memory-audit/20260921/sizes.txt`에 있다.

| 항목 | 크기 |
|---|---:|
| `csi_record_t` | 540B |
| `csi_frame_t` | 2,328B |
| `csi_phase_config_t` | 4,184B |
| `csi_phase_result_t` | 1,136B |
| `csi_amp_result_t` | 26,680B |
| `double complex` | 16B |

`csi-rx/main/csi-rx.c`의 큐는 32 × 540 = 17,280B의 데이터 저장소를 요구한다. 콜백/출력 태스크의 static 레코드 2개 1,080B, base64 배열 685B, 출력 태스크 스택 4,096B까지 단순 합계 23,141B (약 22.6KiB)다. 큐 제어 구조체, TCB, allocator overhead, 기타 전역변수는 제외했다. static 부분은 부팅 힙에서 이미 제외되므로 초기 힙에서 이 합계 전체를 다시 빼면 중복 차감이다.

Wi-Fi 설정은 static RX buffer 10개, dynamic RX/TX 각각 최대 32개다. 로컬 IDF `components/esp_wifi/Kconfig`에 따르면 static RX buffer는 개당 약 1.6KB다. 동적 버퍼는 최대 개수를 항상 사용하는 것이 아니며 Wi-Fi/LwIP/FreeRTOS 전체 사용량을 이 숫자만으로 확정할 수 없다.

## 처리 윈도우별 주요 버퍼 예산

가정: 시작·끝을 포함한 100Hz 균일 입력, N=초×100+1, 누락 없음, 진폭 117열/위상 114열, 현재 기본 FIR 201 taps. MiB=2^20B. PC RSS 측정이 아니라 소스의 동시 할당 구조를 계산한 값이다.

| 윈도우 | 입력 frames | 진폭 주요 동시 보유량 | 위상 보간 시 동시 보유량 |
|---|---:|---:|---:|
| 20초 | 4.44MiB | 6.17MiB | 11.43MiB |
| 30초 | 6.66MiB | 9.28MiB | 17.15MiB |
| 60초 | 13.32MiB | 18.62MiB | 34.29MiB |
| 120초 | 26.64MiB | 37.29MiB | 68.58MiB |

- 입력: N×2,328B. 실제 열이 117개여도 `csi_frame_t`는 256열 최대 배열을 갖는다.
- 진폭: 입력 + N×117×4B 보간 배열 + (N−200)×117×4B FIR 결과 + 결과 구조체 26,680B + FIR 계수 804B. 다운샘플링은 기존 filtered 배열 안에서 수행하며 할당량 자체를 줄이지 않는다. FFT/ACF 작업은 일부 배열 해제 후 수행한다.
- 위상: 입력 + N×8B 시간 + N×8B 인덱스/간격 + N×114×16B 보정 복소 배열 + 동일 크기의 보간 복소 배열. 두 복소 배열이 보간 중 동시에 존재한다. 120초에서는 배열 하나만 약 20.88MiB다.
- 표는 현재 PC 호출 방식처럼 입력 frames를 처리 종료까지 유지하는 조건이다. allocator overhead·태스크 스택·펌웨어는 별도다. 위상 FFT/chirp·파형 버퍼는 보정 배열 해제 뒤에 할당된다. 이 범위의 예시에서는 두 복소 행렬의 중첩이 주요 피크를 만든다.
- 두 경로를 순차 실행하면 두 값을 단순 합산하지 않는다. 입력을 공유하고 작업 버퍼 수명을 분리해야 한다.

## 스택도 별도로 줄여야 함

타깃 GCC로 `-Og -fstack-usage` 검사했다(현재 SDK debug 최적화에 맞춘 독립 소스 컴파일이며 최종 펌웨어 링크/런타임 계측은 아님). 기본 진입점에서 위상 회귀까지의 호출 경로에 잡히는 개별 프레임 합은 약 18,960B다:

`csi_phase_process` 4,208 → `csi_phase_process_with_config` 1,104 → `correct_phase` 5,152 → `correct_phase_row` 4,272 → `fit_phase_offsets` 4,224B.

이는 추가 하위 호출과 라이브러리 사용 전의 합이다. `-Os`로도 큰 스택 프레임이 남는다. 현재 출력 태스크 4,096B나 main 태스크 3,584B에서 직접 호출하는 구조는 적합하지 않다. 자동 배열을 작업 공간으로 분리하는 등의 설계와 실제 high-water mark 측정이 필요하다.

## 실제 사용 가능한 RAM 예산을 확정하기 위한 남은 측정

현재 RX 코드에는 heap 계측 출력이 없고 로컬 build에는 최종 app ELF/map도 없다. 제공된 부팅 로그만으로 수집 중 여유를 계산할 수 없다. Wi-Fi/ESP-NOW/CSI 큐 초기화 뒤와 정상 수집 중 다음 항목을 측정해야 한다:

- `heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)`
- `heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)`
- `heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)`
- PSRAM 활성화 검증 시 `heap_caps_get_total_size(MALLOC_CAP_SPIRAM)`
- 처리 태스크 도입 후 stack high-water mark와 큐 dropped 추이

근거 API는 로컬 IDF의 `components/heap/include/esp_heap_caps.h`에 있다. 부팅 힙 273KiB 전체를 처리용 예산으로 잡지 말고, 수집 중 최소 여유와 최대 연속 블록 확인 후 예비 공간을 남겨야 한다. 현재 단계에서는 수집률 변경·다운샘플링·윈도우 축소·PSRAM 사용 등을 구현하거나 결정하지 않았다.

## 산출물

`outputs/memory-audit/20260921/`: 사용자 부팅 로그 발췌, 타깃 구조체 크기 확인 소스/오브젝트, `phase.su`와 `phase-og.su` 스택 분석, 윈도우별 `budget.json`. 이 문서는 소스 예산 및 초기 힙 확인 결과이며 실행 중 RAM 계측 완료 보고가 아니다.

## 후속: RX 빌드 오류 수정과 정적 크기 확인

사용자가 실행한 `idf.py size`는 `amp_downsample()`의 32비트 size_t에서 항상 거짓인 uint64 범위 비교가 `-Werror=type-limits`로 실패했다. `src/amplitude.c`에서 SIZE_MAX가 해당 상한을 넘는 타깃에서만 검사를 컴파일하도록 변경했다. 64비트 호스트의 overflow 검사는 유지한다.

수정 후 `cmake --build csi-rx/build`와 `--target size`가 통과했다. 루트 C 대조 테스트 15개 모두 통과. 펌웨어 플래시는 하지 않았다.

- 실제 bin: 0xd8c60 = 887,904B, 1MiB 앱 파티션에 160,672B(약 15%) 여유.
- size 도구의 total image: 887,517B (bin padding과 구분).
- HP SRAM 정적 사용: 127,045B, 도구상 남음 193,883B. LP SRAM 사용 24B, 남음 16,360B.
- 도구의 SRAM total은 예약 영역과 설정을 반영하므로 물리 384KiB와 다르다. 이 정적 remaining은 부팅 heap_init이나 Wi-Fi 실행 중 free heap과 같은 지표가 아니다.
- 현재 RX는 수집·출력용이다. 이 크기가 호흡 처리 윈도우의 동적 메모리까지 포함하는 것은 아니다. 기존 보드 부팅 로그와 이번 로컬 바이너리를 동일 펌웨어로 취급하지 않는다.

크기 표: `outputs/memory-audit/20260921/rx-size-after-build-fix.txt`.
