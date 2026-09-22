# 하드웨어 상태 기록

작성: 2026-09-16

> 2026-09-22 루트 `docs/`로 복원했다. 아래는 당시 장비 기록이며 경로는 저장소 루트 기준이다. 실제 보드는 마지막 일반 float RX 펌웨어 그대로이고 이번에는 플래시하지 않았다.

이 문서는 새 케이스 수령 시점의 송·수신기 상태를 정리한다. 각 항목은 근거 종류를 구분해 표시한다.

- **[사용자]** — 사용자가 말한 내용. 장비에서 확인한 값이 아니다.
- **[소스]** — 이 저장소의 펌웨어 소스·설정 파일에 적힌 값. 현재 보드에 올라간 펌웨어의 동작값이라는 근거는 아니다.
- **[빌드]** — 로컬 빌드 산출물에 남은 값. 마지막 로컬 빌드 상태이며 플래시 여부와 무관하다.
- **[미확인]** — 아직 근거가 없다.

## 1. 새 케이스와 장비 배치

| 항목 | 값 | 근거 |
| --- | --- | --- |
| TX/RX 칩 | ESP32-C5 2대 | [사용자] / [소스] `csi-tx/sdkconfig:858`, `csi-rx/sdkconfig:858` (`CONFIG_IDF_TARGET="esp32c5"`) |
| 이전 배치 | TX·RX가 같은 케이스 안 | [사용자] |
| 새 케이스 | 2026-09-16 수령 | [사용자] |
| 새 케이스 장착 여부 | 미확인(장착 작업 보고가 없다는 사실은 미장착의 근거가 아니다) | [미확인] |
| 케이스 재질·차폐 구조 | — | [미확인] |
| 케이스 내부 안테나 간격 | — | [미확인] |
| 지정 포트 | RX `COM3`, TX `COM4` | [사용자] |
| TX/RX 플래시 | 2026-09-16 기본 설정으로 TX·RX 모두 플래시 완료 | [사용자] |

포트 이름은 사용자가 지정한 역할 배정이다. 실행 전 실제 연결을 다시 확인하고, 자동 포트 검색 결과만으로 TX/RX 역할을 재지정하지 않는다.

## 2. 무선 설정 (소스 기준)

두 소스의 무선 파라미터는 동일하게 맞춰져 있다.

| 항목 | 값 | TX 근거 | RX 근거 |
| --- | --- | --- | --- |
| 대역 | 5GHz 전용 (`WIFI_BAND_MODE_5G_ONLY`) | `csi-tx/main/csi-tx.c:18`, `:63` | `csi-rx/main/csi-rx.c:33`, `:90` |
| 채널 | 36, 보조채널 위(`WIFI_SECOND_CHAN_ABOVE`) | `csi-tx.c:16`, `:86-89` | `csi-rx.c:31`, `:106-109` |
| 대역폭 | HT40 (`WIFI_BW40`, 2G/5G 모두) | `csi-tx.c:23-24`, `:72-76` | `csi-rx.c:34-35`, `:94-96` |
| 프로토콜 | `WIFI_PROTOCOL_11N` | `csi-tx.c:26-27`, `:65-70` | `csi-rx.c:36-37`, `:91-93` |
| ESP-NOW phymode / rate | `WIFI_PHY_MODE_HT40` / `WIFI_PHY_RATE_MCS0_LGI` | `csi-tx.c:29-30`, `:98-102` | `csi-rx.c:39-40`, `:117-123` |
| 전력 절약 | `WIFI_PS_NONE` | `csi-tx.c:78` | `csi-rx.c:98` |
| 송신 목표 주기 | 100Hz (`vTaskDelayUntil`로 주기 고정) | `csi-tx.c:39`, `:135-146` | — |
| 스케줄러 틱 | TX `CONFIG_FREERTOS_HZ=1000`, RX `=100` | `csi-tx/sdkconfig:2587` | `csi-rx/sdkconfig:2590` |
| 콘솔 시리얼 속도 | TX 115200, RX 921600 | `csi-tx/sdkconfig:2175` (`UART_DEFAULT`, `:2155`) | `csi-rx/sdkconfig:2178` (`UART_CUSTOM` 0번, `:2156`, `:2167-2171`) |
| CPU 클럭 | 240MHz | `csi-tx/sdkconfig:2188` | `csi-rx/sdkconfig:2191` |
| 플래시 크기 설정 | 2MB | `csi-tx/sdkconfig:1176` | `csi-rx/sdkconfig:1176` |

100Hz는 소스에 적힌 **목표 송신 주기**다. 실제 수신률·패킷 손실은 측정으로 확인하기 전까지 알 수 없다. TX 소스 `csi-tx.c:31-38`의 주석(정합 적분 이득, UART 여유 ~41%, 큐 리팩터로 손실 제거)은 현재 장비에 대한 실측 근거로 쓰지 않는다.

### MAC 주소

| 항목 | 값 | 근거 |
| --- | --- | --- |
| TX STA MAC (강제 설정) | `1a:00:00:00:00:00` | [소스] `csi-tx.c:48-49`, `:91` |
| TX의 ESP-NOW 대상 MAC | `10:bd:a3:d6:5f:40` (유니캐스트 피어) | [소스] `csi-tx.c:45-46`, `:120-126` |
| RX MAC 강제 설정 | 하지 않음(`esp_wifi_set_mac` 주석 처리) → 공장 MAC 유지 | [소스] `csi-rx.c:111` |
| RX의 CSI 소스 필터 | 송신 MAC이 `1a:00:00:00:00:00`인 패킷만 처리 | [소스] `csi-rx.c:57-58`, `:131-133` |
| RX ESP-NOW 피어 | 브로드캐스트 `ff:ff:ff:ff:ff:ff` | [소스] `csi-rx.c:299-304` |
| `10:bd:a3:d6:5f:40`가 현재 RX 보드의 MAC인지 | — | [미확인] |

TX는 위 대상 MAC으로 유니캐스트하고 RX는 MAC 강제 설정을 하지 않으므로, 두 보드를 교체·재배치했다면 대상 MAC 일치 여부를 확인해야 한다. 현재 확인 기록은 없다.

## 3. TX 출력 설정

| 항목 | 값 | 근거 |
| --- | --- | --- |
| 소스의 출력 setter | `esp_wifi_set_max_tx_power(32)` 호출이 **주석 처리**되어 있음 | [소스] `csi-tx.c:104-105` |
| PHY 상한 설정 | `CONFIG_ESP_PHY_MAX_WIFI_TX_POWER=20`, `CONFIG_ESP_PHY_MAX_TX_POWER=20` (TX·RX 동일) | [소스] `csi-tx/sdkconfig:2051`, `:2053`, `csi-rx/sdkconfig:2051`, `:2053` |
| 현재 보드의 동작 출력값 | — | [미확인] |
| 실제 복사 RF 출력 측정 | — | [미확인] |

구분해야 할 세 가지다.

1. 소스의 setter 호출: 지금은 주석 처리 상태이므로 이 코드로 빌드하면 출력을 명시적으로 지정하지 않는다.
2. `CONFIG_ESP_PHY_MAX_*_TX_POWER=20`: PHY **상한** 설정값이며 실제 송신 출력과 같지 않다.
3. 실제 RF 출력: 별도 확인이 필요하다.

사용자는 2026-09-16 출력 비교 실험을 나중으로 미루고 기본 출력 설정 그대로 **TX·RX 양쪽 플래시를 완료했다고 보고했다**[사용자]. 이는 사용자 보고이며 장비 로그로 확인한 값이 아니다. 런타임 게터의 dBm 값과 각 보드에 올라간 펌웨어의 소스 동일성은 별도로 확인하지 않았다. 따라서 현재 보드의 실제 동작 출력값은 여전히 미확인이며, 특정 dBm 값으로 단정하지 않는다. 과거 삭제된 8dBm 기록은 현재 설정의 근거가 아니다(`PROGRESS.md:28`).

## 4. RX의 CSI 수집·게인·기록 방식 (소스 기준)

| 항목 | 값 | 근거 |
| --- | --- | --- |
| CSI 획득 설정 | `enable=true`, `acquire_csi_legacy=false`, `ht20=true`, `ht40=true`, `vht/su/mu/dcm/beamformed=false` | `csi-rx.c:257-270` |
| `acquire_csi_force_lltf` | 0 (`CSI_FORCE_LLTF`) | `csi-rx.c:43`, `:259` |
| `he_stbc_mode` / `val_scale_cfg` / `dump_ack_en` | 2 / 0 / false | `csi-rx.c:267-269` |
| 프로미스큐어스 모드 | 사용 | `csi-rx.c:254` |
| 게인 제어 | `CONFIG_GAIN_CONTROL 1` (활성) | `csi-rx.c:32`, `:143-161` |
| 게인 강제 고정 | `CONFIG_FORCE_GAIN 0` (비활성) | `csi-rx.c:41`, `:153-156` |
| 게인 기준선 | 첫 100패킷 기록 후 101번째에서 baseline 취득 | `csi-rx.c:148-152` |
| 보정 계수 적용 위치 | 보드는 `compensate_gain`을 **보고만** 하고, 실제 적용은 호스트 몫 (원본 int8 보존) | `csi-rx.c:64-65`, `:158-160`, `:172` |
| 원본 CSI 기록 | 보정 없는 raw int8 버퍼를 base64로 출력 | `csi-rx.c:75`, `:189-215`, `:240` |
| 패킷당 버퍼 상한 | `CSI_BUF_MAX 512`바이트, 초과분은 잘라냄. `len` 필드에는 `min(info->len, 512)`로 제한된 **복사·출력 길이**가 들어가며, 잘리기 전 원래 길이는 남지 않는다 | `csi-rx.c:49`, `:164-167`, `:181` |
| 출력 경로 | CSI 콜백은 큐에 넣기만 하고, 우선순위 2 저우선 태스크가 UART로 출력 | `csi-rx.c:45-48`, `:220-247`, `:252` |
| 큐 길이 | `CSI_QUEUE_LEN 32` | `csi-rx.c:51`, `:250` |
| 손실 계수 | 큐 오버플로 시 `s_dropped` 증가, 각 레코드의 `dropped` 열로 출력 | `csi-rx.c:79`, `:171`, `:184-186`, `:245` |
| 시간 기준 | `rx_ctrl.timestamp` (하드웨어 캡처 시각) | `csi-rx.c:63`, `:170` |
| seq | TX 페이로드의 카운터(offset 15)에서 읽음 | `csi-rx.c:62`, `:169`; TX 측 카운터는 `csi-tx.c:138-140` |

CSV 헤더는 `type,seq,rssi,noise_floor,fft_gain,agc_gain,channel,local_timestamp,sig_len,rx_format,len,first_word,compensate_gain,dropped,data`이다(`csi-rx.c:234-236`). `rx_format` 열에는 `rx_ctrl.cur_bb_format`이 들어간다(`csi-rx.c:178`, `:244`). `rssi`·`agc_gain`·`fft_gain`은 모두 RX 수신 측 값이며 TX 출력을 역산하는 근거가 아니다.

`dropped`는 **큐 오버플로로 보드에서 버린 레코드 수**다. 무선 구간 패킷 손실이나 수신률과 같지 않으므로 분리해서 해석한다.

## 5. 빌드와 의존성

| 항목 | 값 | 근거 |
| --- | --- | --- |
| ESP-IDF | v6.0.3 (`C:/esp/v6.0.3/esp-idf`) | [빌드] `csi-tx/build/project_description.json:6`, `:14`; `csi-rx/build/project_description.json:6`, `:14` / [소스] `csi-rx/dependencies.lock:34-37` |
| 빌드 타깃 | `esp32c5` | [빌드] 두 `project_description.json:15` / [소스] `csi-rx/dependencies.lock:42` |
| 마지막 로컬 빌드 버전 | TX `e653ce5`, RX `e653ce5-dirty` | [빌드] `csi-tx/build/project_description.json:4`, `csi-rx/build/project_description.json:4` |
| 모니터 baud | TX 115200, RX 921600 | [빌드] 두 `project_description.json:20` |
| 툴체인 | `riscv32-esp-elf` esp-15.2.0_20251204 | [빌드] 두 `project_description.json:22` |
| RX 의존성 선언 | `idf >=4.4.1`, `espressif/esp_csi_gain_ctrl ^0.1.5` | [소스] `csi-rx/main/idf_component.yml:19-21` |
| RX 의존성 실제 고정 | `esp_csi_gain_ctrl 0.1.5` (hash `2700b662…`), `cmake_utilities 0.5.3` | [소스] `csi-rx/dependencies.lock:2-33` |

RX 빌드 버전이 `-dirty`인 것은 마지막 빌드 시점에 커밋되지 않은 변경이 있었다는 뜻이다. 빌드 산출물은 **로컬에서 마지막으로 빌드한 상태**를 말해줄 뿐, 지금 각 보드에 올라가 있는 펌웨어와 같다는 근거가 아니다.

## 6. 미확인 항목

| 항목 | 상태 |
| --- | --- |
| 새 케이스 장착 여부, 재질, 차폐 구조 | 미확인 |
| 케이스 내부·장착 후 안테나 간격과 방향 | 미확인 |
| TX 실제 동작 출력값(API 설정값·실측 RF 모두) | 미확인 |
| 각 보드에 현재 올라가 있는 펌웨어 버전과 소스 동일성 | 미확인(플래시 완료는 사용자 보고, 부팅 펌웨어 식별은 별도 확인 필요) |
| `10:bd:a3:d6:5f:40`와 현재 RX 보드 MAC의 일치 | 미확인 |
| 실제 수신률, 패킷 손실, `dropped` 발생 정도 | 미측정 |
| COM3/COM4의 현재 실제 연결 상태 | 실행 전 재확인 필요 |
| 측정 장소, 거리, 사람 유무, 호흡 기준 확보 방법 | 미정 |

## 7. 이 문서를 쓸 때의 주의

- 소스 상수와 `sdkconfig` 값은 **저장소 상태**다. 보드 동작값으로 쓰려면 별도 확인이 필요하다.
- 소스 주석의 통신률·정확도 서술은 실측 근거가 아니다.
- 미확인 항목은 확인 방법이 서로 다르다. 동작 출력값·펌웨어 식별처럼 보드 런타임 설정에 해당하는 항목은 부팅 로그나 게터 출력 확인이 필요하고, 그 실행에는 포트 열기와 별도 요청·준비 확인이 필요하다. 케이스 장착 여부·재질·안테나 간격은 포트와 무관한 물리적 확인 사항이다.
- 이 문서는 읽기 전용 조사 결과와 사용자 보고만 담는다.
