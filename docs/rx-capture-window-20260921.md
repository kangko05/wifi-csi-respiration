# RX 수신·큐·60초 윈도우 구현 정리

작성일: 2026-09-21. 현재 RX 소스와 사용자가 제공한 보드 실행 로그 기준.

후속 변경: 진폭 분석의 메모리 절약 경로와 RX 연결을 추가했다. 아래는 수집 단계 완료 시점의 기록이며, 최신 분석 동작·검증은 [RX 진폭 분석 문서](rx-amplitude-20260921.md)를 참고한다.

## 1. 현재 완료한 범위

ESP32-C5 RX에서 **CSI 수신 → 레코드 복사 → 큐 → 최근 60초 원시 CSI 윈도우**까지 연결하고 실제 수신을 확인했다. PSRAM 8MB 인식, 메모리 검사, 윈도우 할당도 성공했다.

현재 1초마다 출력하는 것은 윈도우 상태다. **보드에서 호흡 추정이나 BPM 출력은 아직 하지 않는다.** 기존 PC용 진폭·위상 알고리즘을 그대로 보드에서 실행할 수 있다는 검증도 아니다.

## 2. 파일별 역할

경로는 `csi-rx/main/` 기준이다.

| 파일 | 역할 |
|---|---|
| `csi-rx.c` | `app_main()`에서 단계별 초기화와 최종 오류 처리 |
| `constants_rx.h` | 채널, PHY, 큐 길이, 윈도우 시간·용량 설정 |
| `init_wifi.c/.h` | 기본 Wi-Fi와 ESP-NOW 초기화 |
| `queue.c/.h` | `csi_record_t` 200개를 담는 FreeRTOS 큐 생성 |
| `csi_capture.c/.h` | CSI 설정, 콜백 등록, 수신 데이터를 큐로 복사 |
| `csi_process.c/.h` | PSRAM 할당, 큐 소비 태스크, 윈도우 갱신·상태 출력 |
| `csi_window.c/.h` | 시간 기준 만료와 원형 인덱스를 사용하는 원시 데이터 윈도우 |
| `CMakeLists.txt` | 위 소스 파일을 펌웨어 빌드에 등록 |

공통 레코드는 [`include/csi_resp/csi_record.h`](../include/csi_resp/csi_record.h)에 있다. 과거 수신 코드 `csi-rx.c.old`는 빌드 대상이 아니다.

설정 매크로는 ESP-IDF의 `CONFIG_*`와 구별하도록 `CSI_RX_*` 접두사로 통일했다. 로그용 `TAG`와 수신 MAC 필터 등 파일 내부에서만 쓰는 값은 해당 `.c`의 `static`으로 둔다.

## 3. 시작 순서와 오류 처리

```text
NVS 초기화·오류 검사
  → init_wifi_base()
  → init_wifi_esp_now(peer)
  → create_queue(&s_csi_queue)
  → start_csi_process(s_csi_queue)
  → init_wifi_csi(s_csi_queue)
```

수신을 켜기 전에 큐와 소비 태스크를 준비한다. `QueueHandle_t` 자체가 핸들이므로 이미 생성된 큐를 전달할 때는 포인터를 한 단계 더 붙이지 않는다. `create_queue()`만 호출자에게 핸들을 써주기 위해 `QueueHandle_t *`를 받는다.

내부 초기화 함수는 실패하면 `esp_err_t`를 반환하고, `app_main()`에서 `ESP_ERROR_CHECK()`로 중단 여부를 처리한다. Wi-Fi 초기화의 부분 성공 상태를 되돌리는 정리나 자동 재시도는 구현하지 않았다. 처리 태스크 생성 실패 시에는 할당한 윈도우 메모리를 해제한다.

## 4. 데이터 흐름과 소유권

```text
TX: 약 100Hz로 ESP-NOW 송신
        ↓
Wi-Fi 태스크: CSI 수신 콜백
  검사 → csi_record_t 작성 → xQueueSend(..., 0)
        ↓ 레코드 전체 복사
FreeRTOS 큐: 최대 200개
        ↓ xQueueReceive(): 꺼낸 레코드는 큐에서 제거
처리 태스크: csi_process
  원시 레코드를 60초 윈도우에 복사
  → 오래된 항목 제거
  → 약 1초마다 상태 출력
```

콜백은 송신 MAC, 포인터, CSI 길이와 payload 길이를 검사한다. 현재 송신 MAC 필터는 `1a:00:00:00:00:00`이다. CSI 길이는 0이 아니며 짝수이고 512바이트 이하여야 한다. 시퀀스는 기존 프로젝트 형식의 payload offset 15에서 4바이트를 `memcpy()`로 읽는다. 이 형식이 달라지면 파싱도 수정해야 한다.

레코드에는 시퀀스, 타임스탬프, RSSI, 게인, 채널, 유효 길이, 원시 I/Q 등을 저장한다. 처음 100개 유효 패킷으로 게인 기준값을 수집하고 이후 기준값을 계산한다. 게인 보상값은 메타데이터로 보관하며 원시 I/Q에는 직접 적용하지 않는다.

콜백 인자의 `info`와 내부 데이터 포인터는 반환 후 보관하지 않는다. 큐에는 데이터 전체가 복사되므로 콜백의 임시 레코드를 다음 수신에 재사용해도 된다. 큐가 차면 기다리지 않고 패킷을 버리며 누적 드롭 수를 올린다.

**60초 윈도우는 처리 태스크 하나만 읽고 수정한다.** 수신 콜백은 윈도우에 접근하지 않는다. 앞으로 분석도 같은 태스크에서 수행하면 분석 도중 수신 측이 윈도우를 덮어쓰지 않는다. 분석하는 동안 새 패킷은 앞단 큐에 쌓인다.

## 5. 윈도우 유지 방식

- 시간 범위: 최근 **60초**, `(현재 시각 - 60초, 현재 시각]`.
- 최대 저장량: **6,600개**. 100Hz × 60초에 10% 용량 여유를 둔다.
- 갱신: 패킷을 꺼낼 때마다 수행한다. 1초 주기는 상태 출력 주기다.
- 자료구조: 배열, 가장 오래된 항목의 인덱스 `head`, 저장 개수 `count`.
- 시간 만료: 수신 타임스탬프로 판단하며, 오래된 항목을 빼고 새 항목을 추가한다.
- 수신 중단: 큐 수신 대기 시간은 100ms이고, 비어 있을 때 로컬 경과 시간으로 만료를 진행한다.
- 용량 초과: 아직 60초가 지나지 않은 항목도 제거해야 한다면 `capacity_drops`를 증가시킨다.
- 타임스탬프: `uint32_t` 순환은 unsigned 차이로 처리한다. 정상적인 연속 시각을 전제로 하며 역행 입력은 거부한다.

`csi_window_at(window, 0)`은 가장 오래된 항목을 반환한다. 반환 포인터는 다음 윈도우 변경 전까지만 사용해야 한다. 수신률이 바뀌면 개수는 달라질 수 있으며, 6,600개가 찼는지를 분석 준비 조건으로 쓰면 안 된다.

## 6. 메모리와 PSRAM 설정

현재는 `buf[512]`를 포함한 **540바이트 `csi_record_t` 전체**를 저장한다. 실제 유효 CSI가 더 짧아도 슬롯 크기는 줄어들지 않는다. 복소 `float`/`double` 배열이나 base64로 윈도우를 저장하지 않는다.

| 용도 | 데이터 저장소 크기 |
|---|---:|
| 큐 200개 | 108,000B ≈ 105.5KiB |
| 윈도우 6,600개 | 3,564,000B ≈ 3.40MiB |
| 처리 태스크 스택 설정 | 4,096B |

큐 제어 구조체, Wi-Fi 버퍼, 기타 정적 데이터, 할당 관리 비용은 별도다. 윈도우는 `heap_caps_malloc(..., MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)`으로 PSRAM에 명시적으로 할당한다. 큐의 실제 메모리 위치는 실행 로그만으로 확인하지 않았다.

현재 `sdkconfig`에서 확인한 설정:

```text
CONFIG_SPIRAM=y
CONFIG_SPIRAM_MODE_QUAD=y
CONFIG_SPIRAM_SPEED_40M=y
CONFIG_SPIRAM_BOOT_INIT=y
CONFIG_SPIRAM_USE_MALLOC=y
CONFIG_SPIRAM_MEMTEST=y
```

실제 선택된 접근 방식은 **일반 malloc에도 PSRAM을 허용하는 방식**이다. 앞서 설명한 `SPIRAM_USE_CAPS_ALLOC` 방식과는 다르지만 명시적 PSRAM 윈도우 할당은 두 방식 모두 지원한다. 전체 PSRAM에서 윈도우 크기만 빼서 실제 여유 힙이라고 단정하면 안 된다.

사용자 제공 부팅 로그:

```text
esp_psram: Found 8MB PSRAM device
esp_psram: Speed: 40MHz
esp_psram: SPI SRAM memory test OK
esp_psram: Adding pool of 8192K of PSRAM memory to heap allocator
csi-queue: queue created (200 records, 540 bytes per record)
csi-process: raw window allocated: 3564000 bytes in PSRAM
```

이 로그로 PSRAM 탑재·활성화·할당이 확인됐다. 별도로 물리 플래시는 8MB인데 이미지 설정은 여전히 2MB이고 앱 파티션은 1MiB다. 해당 플래시 경고와 PSRAM 성공 여부는 별개다.

## 7. 실제 수신 확인과 로그 해석

처음 `window=0`이 계속 출력된 원인은 TX 보드가 연결되지 않았기 때문이었다. TX 연결 후 초당 약 100개씩 늘고 60초에서 유지되는 것을 확인했다.

```text
window=5986/6600 span=59849ms queued=0 queue_drops=0 capacity_drops=0 rejected=0
window=6001/6600 span=59999ms queued=0 queue_drops=0 capacity_drops=0 rejected=0
window=6000/6600 span=59990ms queued=0 queue_drops=0 capacity_drops=0 rejected=0
```

| 항목 | 의미 |
|---|---|
| `window` | 현재 저장 개수 / 최대 용량 |
| `span` | 가장 오래된 패킷부터 최신 패킷까지의 시간 차이 |
| `queued` | 로그 시점에 큐에 남아 있는 개수 |
| `queue_drops` | 최근 수용 레코드가 전달한 큐 포화 누적 드롭 수 |
| `capacity_drops` | 윈도우 용량 부족으로 강제 제거한 누적 개수 |
| `rejected` | 처리 태스크에서 윈도우 삽입을 거부한 누적 개수 |

6,000~6,001개 차이는 실제 패킷 시간 간격에 따른 변화다. 오래된 데이터가 만료되며 최신 약 60초를 유지하고 있다. `queued=0`은 그 시점에 큐가 비어 있다는 뜻이며, 실행 중 최대 사용량을 나타내지는 않는다. 콜백 내부 필터에서 버린 패킷은 `rejected`에 포함되지 않으므로 이 로그만으로 무선 구간 전체 무손실을 증명하지는 않는다.

## 8. 검증과 실행 명령

### 런타임 힙 사용량 확인

후속으로 처리 태스크 시작 시와 5초마다 내부 RAM·PSRAM 힙을 별도로 출력하도록 추가했다. 새 펌웨어를 플래시한 뒤 다음 형태의 두 줄을 확인한다.

```text
heap internal (bytes): allocated=... free=... min_free=... largest=...
heap psram (bytes): allocated=... free=... min_free=... largest=...
```

`allocated`는 현재 할당된 힙 바이트, `free`는 현재 가용 힙, `min_free`는 해당 힙들의 최소 가용량 기록 합계, `largest`는 한 번에 할당 가능한 가장 큰 연속 블록이다. `allocated`에는 힙에서 할당된 태스크 스택 등도 포함되지만 정적 데이터나 코드 영역은 포함되지 않는다. 따라서 전체 물리 RAM 사용량과는 구분한다. 이 로그 추가 후의 실제 수치는 아직 보드 로그로 확인하지 않았다.

### 기존 검사와 재실행

작업 중 RX 전체 빌드가 통과했다. 콜백 모의 검사에서 포인터·길이·MAC 검사, 데이터 복사, 큐 포화, 게인 기준값 수집 흐름을 확인했다. 호스트 윈도우 테스트에서는 저장 순서, 원본과의 독립성, 시간 만료, 용량 초과, 긴 간격, 타임스탬프 순환, 잘못된 입력을 검사했고 통과했다. 이 테스트가 호흡 정확도나 분석 실행 시간을 검증하는 것은 아니다.

저장된 윈도우 테스트 재실행, 저장소 루트에서:

```bash
cc -std=c11 -Wall -Wextra -Werror -fsanitize=undefined \
  -Iinclude -Icsi-rx/main \
  tests/test_csi_window.c csi-rx/main/csi_window.c \
  -o /tmp/test_csi_window
/tmp/test_csi_window
```

ESP-IDF 환경이 활성화된 터미널에서:

```bash
cd csi-rx
idf.py menuconfig
idf.py build
# 보드와 포트를 확인한 후 필요할 때 실행
idf.py -p /dev/ttyUSB0 flash monitor
```

PSRAM 메뉴는 `Component config → ESP PSRAM`이다. 현재 콘솔 속도는 115200이다. `idf.py monitor`만 실행하면 기존 펌웨어를 관찰하며 새 펌웨어를 플래시하지 않는다.

개발 환경에서는 `.clangd`에 RX/TX별 compilation database 경로를 지정하고, clangd가 지원하지 않는 `-mtune=*`를 제거했다. Neovim clangd의 `--query-driver`도 Linux의 Espressif RISC-V GCC 경로로 변경해 RX 파일 분석과 헤더 자동완성을 확인했다.

## 9. 다음 작업과 남은 제약

1. 60초 윈도우를 분석하는 경로를 추가한다. 원시 데이터를 유지하고 필요한 패킷·서브캐리어만 임시 복소수로 변환하는 방향이다.
2. 기존 PC 알고리즘의 전체 중간 배열을 그대로 할당하지 않도록 변경하고 수치 결과를 대조한다. 기존 메모리 산정상 60초 진폭 약 18.6MiB, 위상 약 34.3MiB여서 8MB PSRAM에 그대로 들어가지 않는다.
3. 분석이 붙은 뒤 실행 시간, 큐 최대 사용량, 드롭, PSRAM 가용량·최대 연속 블록을 측정한다. 200개 큐는 100Hz에서 약 2초분이지만 평균 처리 속도가 수신 속도를 따라가지 못하면 결국 넘친다.
4. 1초마다 추정을 갱신하려면 계산 시간과 최초 윈도우 준비 조건, 누락·빈 방 등 결과 유효성 판단을 정의해야 한다.
5. 진폭·위상 결과 선택과 CIR은 이후 범위다. 현재 수집 성공만으로 호흡 검출 성능을 판단하지 않는다.

관련 기존 기록: [RX 메모리 산정](rx-memory-budget-20260921.md), [CIR 메모리 산정](cir-memory-budget-20260921.md), [Linux 검증](linux-validation-20260921.md). 이전 메모리 문서의 “PSRAM 활성화 미확인” 상태는 이번 보드 로그로 해소됐지만, 분석 작업 공간 문제는 남아 있다.
