`phase.c`는 **복소 CSI를 보정한 뒤, 주파수 분석과 시간 파형의 피크 계수를 비교해 호흡 주기 후보를 만드는 코드**입니다. 이름은 phase지만 `atan2()`로 얻은 위상값만 분석하는 구조는 아니고, 실수부·허수부를 가진 복소 CSI를 계속 사용합니다.

총 **34개 함수**가 있습니다. 코드 순서보다 이해하기 쉬운 처리 흐름 순서로 설명하겠습니다.

먼저 사용하는 데이터 표기입니다.

- `frames`: 전처리된 CSI 패킷 배열. 각 패킷에 실수부, 허수부, 유효성 마스크, 타임스탬프가 있습니다.
- `h`: `h[시간 인덱스 * 열 개수 + 서브캐리어 인덱스]` 형태의 복소 CSI 배열입니다.
- `n`: 시간 샘플 수입니다.
- `fs`: 초당 샘플 수입니다.
- `p`: 설정에서 계산한 실제 사용 열 목록과 주파수 격자 크기입니다.
- `r`: 추정 결과를 담는 구조체입니다.

전체 흐름은 다음과 같습니다.

```text
csi_phase_process()
  └─ csi_phase_process_with_config()
       ├─ 설정 검증
       ├─ prepare_phase_window()   입력 검사·시간축 준비
       ├─ correct_phase()          RMS 정규화·위상 보정
       ├─ interpolate_phase()      균일 시간축으로 보간
       └─ analyze()
            ├─ 복소 평균 제거
            ├─ 에너지 높은 열 선택 → 실수 파형 합성 → 필터
            ├─ 복소 스펙트럼 → 주파수 후보
            ├─ 후보 주변으로 파형 재필터 → 피크 계수
            └─ 두 추정치의 차이와 sharpness로 채택 판정
```

**1–3. 외부에서 호출하는 함수**

**1. [`csi_phase_default_config()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:22)**

기본 설정 구조체를 만들어 반환합니다.

| 설정                    |                      기본값 | 의미                             |
| ----------------------- | --------------------------: | -------------------------------- |
| `n_bins`                |                         117 | 입력 CSI 열 개수                 |
| `subcarrier_spacing_hz` |                   312,500Hz | 서브캐리어 간격                  |
| `bins[k].frequency_hz`  |           `(k - 58) × 간격` | 각 열의 주파수 오프셋            |
| 열 역할                 | 57~59는 NULL, 나머지는 USED | 실제 분석 열은 114개             |
| 탐색 대역               |                    0.05~1Hz | 3~60bpm                          |
| 주파수 간격             |                     0.005Hz | 후보 격자 간격 0.3bpm            |
| `top_columns`           |                           5 | 시간 파형을 합칠 때 선택할 열 수 |
| `whiten_bins`           |                          61 | 스펙트럼 배경 중앙값 창 크기     |
| `max_gap_s`             |                       0.1초 | 허용하는 최대 데이터 공백        |
| `min_duration_s`        |                        20초 | 필요한 최소 관측 길이            |
| `max_null_ratio`        |                        0.05 | NULL/USED 평균 크기 비율 상한    |
| `min_sharpness`         |                        1.55 | 스펙트럼 피크 선명도 기준        |
| `max_agreement_bpm`     |                        3bpm | 두 추정치의 차이 기준            |

이 배치는 **기존 HT40 해석을 기본값으로 넣은 것**입니다. 실제 장비 배치를 자동으로 확인하는 함수는 아닙니다.

**2. [`csi_phase_process()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:1040)**

가장 간단한 공개 진입점입니다.

```c
csi_phase_result_t result;
int status = csi_phase_process(frames, count, &result);
```

기본 설정을 만든 뒤 `csi_phase_process_with_config()`에 전달합니다. 자체 계산은 없습니다.

**3. [`csi_phase_process_with_config()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:972)**

전체 파이프라인을 실행하고 메모리를 관리하는 핵심 공개 함수입니다.

1. 설정을 검증하고 내부 파라미터를 만듭니다.
2. 입력 포인터·패킷 수·배열 크기를 확인합니다.
3. 결과 구조체를 초기화합니다.
4. 입력 윈도우를 준비합니다.
5. 위상을 보정합니다.
6. 균일 시간축으로 보간합니다.
7. 분석 결과를 계산합니다.
8. 중간 버퍼를 해제합니다.

반환값과 채택 여부를 구분해야 합니다.

```c
status == 0          // 계산 완료. 후보가 보류됐을 수도 있음
status == 1          // 입력·설정·메모리 할당·처리 오류
result.accepted == 1 // 현재 기준을 통과한 주기 후보
```

오류 반환 시 결과 구조체가 완성됐다고 가정하면 안 됩니다. 일부 초기 오류는 결과 초기화보다 먼저 반환합니다.

**4–7. 설정 검증과 열 매핑**

**4. [`phase_config_valid()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:46)**

설정의 기본적인 유효성을 검사합니다.

- 입력 열 수가 배열 용량 안에 있는가?
- 주파수·간격 등이 유한한 값인가?
- `0 < min_hz < max_hz`인가?
- 주파수 간격과 선택 열 수가 양수인가?
- 중앙값 창 크기가 양의 홀수인가?
- 시간·판정 기준이 허용 범위인가?

**유효하면 1, 아니면 0**입니다. 오류 시 1을 반환하는 다른 내부 함수들과 반환 규칙이 다릅니다.

**5. [`phase_parameters_init()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:65)**

사용자가 준 설정을 실제 계산용 정보로 바꿉니다.

- USED 열의 원본 인덱스를 `raw_bins[]`에 저장합니다.
- USED 열의 주파수가 중복 없이 오름차순인지 확인합니다.
- NULL 열 수를 셉니다.
- 사용 열이 최소 2개이고 `top_columns`가 그 수를 넘지 않는지 검사합니다.
- 탐색 주파수 개수를 계산합니다.

```text
n_frequencies = floor((max_hz - min_hz) / step_hz) + 1
```

기본값이면 191개입니다. 부동소수점 오차 때문에 포함돼야 할 마지막 격자가 빠지지 않도록 정수 근처의 값을 보정합니다.

**6. [`raw_bin()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:113)**

분석용 열 번호를 원본 CSI 열 번호로 변환합니다.

예를 들어 기본 배치에서는 원본 57~59번이 빠지므로, 분석용 57번 열은 원본 60번 열입니다.

**7. [`omega()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:117)**

해당 서브캐리어의 주파수 오프셋을 각주파수로 변환합니다.

```text
ωk = 2π × frequency_hz
```

여기의 주파수는 **호흡 주파수가 아니라 서브캐리어 주파수 오프셋**입니다. 위상 오차의 기울기를 계산할 때 사용합니다.

**8–11. 입력 검사와 시간축 준비**

**8. [`inspect_phase_frame()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:779)**

패킷 하나를 검사합니다.

- SKIP 열은 무시합니다.
- USED·NULL 열의 실수부·허수부에 NaN/Inf가 있으면 오류입니다.
- NULL과 USED 열의 크기를 각각 누적합니다.
- USED 열 중 하나라도 마스크가 무효라면 그 패킷을 제외합니다.
- 각 USED 열에 유효한 비영점 값이 관측됐는지 `seen[]`에 기록합니다.

반환값은 다음과 같습니다.

```text
-1: 비유한 수치 발견
 0: 분석에서 제외할 패킷
 1: 유지할 패킷
```

`occupied_sum`은 사람의 재실 여부가 아니라 **USED 열의 크기 합**을 뜻합니다.

**9. [`collect_phase_rows()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:819)**

전체 패킷에서 사용할 행과 실제 시간을 모읍니다.

- 입력 열 수가 설정과 일치하는지 확인합니다.
- 장치 타임스탬프 차이를 누적합니다.
- `uint32_t` 뺄셈으로 정상적인 카운터 wrap을 처리합니다.
- 지나치게 큰 시간 차이나 전체 시간 범위를 거부합니다.
- 유효 패킷의 시간과 원본 행 번호를 저장합니다.
- NULL/USED 크기 비율과 `seen[]`을 검사합니다.
- 처음·끝에서 제외된 구간 길이를 최대 공백 계산에 반영합니다.

이 단계에서 `intervals[]`에는 시간 간격이 아니라 **원본 행 번호**가 임시로 들어갑니다.

참고로 타임스탬프 차이가 0인 중복 시각을 여기서 명시적으로 거부하지는 않습니다.

**10. [`prepare_phase_window()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:907)**

실제 분석할 복소 배열과 시간축 정보를 준비합니다.

1. `collect_phase_rows()`를 호출합니다.
2. 유지한 행의 USED 열만 복사해 `h`를 만듭니다.
3. 내부 공백까지 포함해 최대 공백을 계산합니다.
4. 공백과 최소 관측 길이를 검사합니다.
5. 유효 패킷 간 시간 차이의 중앙값으로 `fs`를 구합니다.
6. 탐색 대역이 샘플링 주파수에 비해 너무 높으면 거부합니다.
7. 균일 시간축의 샘플 수를 계산합니다.

```text
dt = median(유효 패킷 간 시간 차이)
fs = 1 / dt
n  = floor((마지막 시간 - 첫 시간) × fs) + 1
```

`intervals[]`는 여기서 원본 행 번호 저장 용도를 마친 뒤 시간 간격 배열로 재사용됩니다.

**11. [`interpolate_phase()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:880)**

불규칙한 수신 시각의 복소 CSI를 일정한 시간 간격으로 보간합니다.

```text
t = 첫 유효 시각 + i / fs
z(t) = z_left + fraction × (z_right - z_left)
```

복소수에 선형 보간하므로 실수부와 허수부를 각각 선형 보간하는 것과 같습니다. 위상 각도를 직접 보간하지 않습니다.

큰 공백을 메우지 않도록 앞 단계에서 최대 공백을 제한합니다.

**12–18. RMS 정규화와 위상 보정**

**12. [`power()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:121)**

복소수 크기의 제곱을 구하는 보조 함수입니다.

```text
power(z) = Re(z)² + Im(z)² = |z|²
```

제곱근을 계산하지 않습니다.

**13. [`wrap_angle()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:125)**

각도를 `[-π, π)` 범위로 되돌립니다.

위상 차이가 ±π 경계를 넘을 때 같은 방향을 나타내는 각도로 정리하는 데 사용합니다.

**14. [`build_phase_target()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:228)**

각 패킷의 크기를 정규화하고, 위상 보정의 기준이 될 복소 평균을 만듭니다.

패킷마다:

1. USED 열 전체의 RMS를 구하고 나눕니다.
2. 이웃한 분석 열 사이의 복소 상관을 합합니다.
3. 그 위상으로 초기 시간 오차 `tau[i]`를 추정합니다.
4. `tau`를 적용한 열 합의 위상에서 공통 회전량 `psi`를 구합니다.
5. 보정한 값을 시간 평균해 `target[k]`에 누적합니다.

주요 식은 다음과 같습니다.

```text
corr = Σ h[k] × conj(h[k+1])
tau  = angle(corr) / (2π × subcarrier_spacing_hz)
```

`h`에는 RMS 정규화가 직접 반영됩니다. 초기 위상 보정값은 `target`을 만들 때 적용합니다.

`target`은 이 윈도우에서 계산한 기준이지, 물리적인 직접 경로만 정확히 추출한 값은 아닙니다.

**15. [`select_phase_columns()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:269)**

위상 오차를 회귀할 때 사용할 열을 고릅니다.

- `target`의 평균 파워를 구합니다.
- 그 평균의 10%보다 큰 파워를 가진 열을 선택합니다.
- 선택한 열이 8개 미만이면 전체 USED 열로 되돌아갑니다.
- 기준 파워가 0 이하면 전체를 선택합니다.

이 선택은 뒤에서 시간 파형을 만들기 위해 고르는 `top_columns`와 **별개의 선택**입니다.

**16. [`fit_phase_offsets()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:296)**

기준과 현재 패킷 사이의 위상 차이에 직선을 맞춥니다.

```text
위상 차이 ≈ slope × 서브캐리어 각주파수 + intercept
```

처리 과정은 다음과 같습니다.

1. 선택 열의 앞뒤 최대 3개씩, 총 최대 7개 값을 합해 주변 기준 각도를 구합니다.
2. 그 기준 각도를 열 방향으로 unwrap합니다.
3. 개별 각도를 주변 기준과 가까운 가지로 맞춥니다.
4. `|w[j]|`를 가중치로 사용해 가중 최소제곱 회귀를 합니다.

`slope`는 주파수에 따라 증가하는 위상 오차를, `intercept`는 모든 열에 공통인 위상 오차를 나타냅니다.

**17. [`correct_phase_row()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:351)**

패킷 하나에 실제 위상 보정을 적용합니다.

먼저 회귀 입력을 만듭니다.

```text
w[k] = conj(row[k]) × target[k] × exp(-jωk tau)
```

그다음 `fit_phase_offsets()`의 기울기와 절편을 사용합니다.

```text
row[k] *= exp(j × (ωk × (tau + slope) + intercept))
```

회귀는 선택한 열로 하지만, 구한 보정은 모든 USED 열에 적용합니다.

**18. [`correct_phase()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:368)**

위상 보정 전체를 묶는 함수입니다.

```text
build_phase_target()
→ select_phase_columns()
→ 각 패킷에 correct_phase_row()
```

패킷별 `tau` 배열을 할당하고 사용 후 해제합니다. 입력 `h`를 직접 수정합니다.

**19–21. 임의 주파수 계산용 Bluestein 변환**

**19. [`chirp_init()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:145)**

지정한 주파수 격자에서 복소 스펙트럼을 계산할 준비를 합니다.

입력은 다음과 같습니다.

- `n`: 시간 샘플 수
- `m`: 계산할 주파수 개수
- `start`: 시작 주파수 / `fs`
- `step`: 주파수 간격 / `fs`

Bluestein 방식으로 주파수 계산을 컨볼루션 형태로 바꾸기 위한 chirp 배열과 커널을 생성합니다. 컨볼루션 길이는 `n + m - 1` 이상인 2의 거듭제곱으로 잡습니다.

커널 FFT는 한 번 계산하고 여러 서브캐리어에 재사용합니다.

**20. [`chirp_apply()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:197)**

준비한 변환을 한 열에 적용합니다.

```text
평균 제거
→ input chirp 곱하기
→ FFT
→ 커널 FFT와 곱하기
→ IFFT
→ output chirp 곱하기
```

계산하는 값은 개념적으로 다음과 같습니다.

```text
X[j] = Σ (x[i] - mean) × exp(-j2π(start + j × step) × i)
```

`stride`는 같은 서브캐리어의 다음 시간 샘플까지의 배열 간격입니다. 결과는 `plan.work[]` 앞부분에 저장됩니다.

**21. [`chirp_free()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:137)**

변환 계획에 속한 네 버퍼를 해제하고 구조체를 0으로 초기화합니다.

**22–26. 평균 제거, 파형 합성, 스펙트럼 후보**

**22. [`remove_phase_mean()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:529)**

각 서브캐리어에서 시간 방향 복소 평균을 뺍니다.

```text
h[t,k] ← h[t,k] - mean_t(h[t,k])
```

정적인 복소 성분을 제거하고 시간에 따라 변하는 성분을 남깁니다. 처리 후 전체 배열의 최대 크기를 반환해 평탄 신호 판정에 사용합니다.

함수 이름과 달리 위상각의 평균을 빼는 함수는 아닙니다.

**23. [`phase_band_energy()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:549)**

각 열에서 탐색 대역에 들어가는 에너지 합을 구합니다.

- `chirp_init(n, n, 0, 1/n)`으로 일반적인 N점 DFT 격자를 계산합니다.
- 양·음 주파수 모두에서 절댓값이 탐색 대역 안인 주파수의 파워를 합합니다.
- DC는 제외합니다.

이 값은 **시간 파형을 만들 열의 순위**를 정하는 데 사용합니다.

**24. [`combine_phase_columns()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:575)**

에너지가 큰 열들을 선택해 하나의 실수 파형으로 합칩니다.

각 열에 대해:

1. 남은 열 중 대역 에너지가 가장 큰 열을 고릅니다.
2. 원본 열 번호를 `selected_bins[]`에 기록합니다.
3. 복소 변동을 실수축에 잘 투영하도록 회전합니다.
4. 첫 번째 열의 파형과 내적해 부호를 맞춥니다.
5. `wave[]`에 더합니다.

회전각은 다음 값에서 구합니다.

```text
second = Σ h[t]²
rotation = exp(-j × angle(second) / 2)
```

평균이 제거된 복소 점들의 변동이 가장 큰 축을 실수축으로 가져오는 계산입니다. 부호 정렬은 반대 방향 파형끼리 상쇄되는 것을 줄입니다.

선택된 열의 `energies`를 `-1`로 바꾸므로 에너지 배열도 수정됩니다. 결과는 평균이 아니라 합입니다.

**25. [`phase_spectrum()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:618)**

설정한 세밀한 주파수 격자에서 모든 USED 열의 스펙트럼을 계산합니다.

```text
spectrum[f] = Σ열 |Σ시간 h[t,열] × exp(-j2πft)|²
```

각 열에서는 시간 방향 복소 합을 하고, 열 사이에서는 그 **파워를 합산**합니다. 서브캐리어 복소값을 먼저 모두 합치는 계산은 아닙니다.

여기는 `top_columns`만이 아니라 모든 USED 열을 사용합니다.

**26. [`select_spectral_peak()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:640)**

주변 배경 대비 가장 두드러지는 주파수 후보를 고릅니다.

1. 각 주파수에서 주변 `whiten_bins`개의 파워 중앙값을 구합니다.
2. 경계 밖 인덱스는 가장 가까운 끝값으로 채웁니다.
3. 너무 작은 분모를 막기 위한 하한을 만듭니다.
4. 원래 파워를 배경 중앙값으로 나눕니다.
5. 그 비율이 최대인 인덱스를 반환합니다.

```text
whitened[f] = spectrum[f] / max(background[f], floor_power)
sharpness  = max(whitened)
```

즉 `sharpness`는 **주변 스펙트럼 배경 대비 피크 비율**입니다. 호흡일 확률이나 검증된 SNR을 의미하지 않습니다.

**27–32. 시간 파형에서 피크 세기**

**27. [`compare_peak()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:395)**

`qsort()`에 전달하는 정렬 비교 함수입니다.

- 높이가 큰 피크를 먼저 둡니다.
- 높이가 같으면 시간 인덱스가 큰 피크를 먼저 둡니다.

가까운 피크끼리 경쟁할 때 우선순위를 정합니다.

**28. [`find_local_peaks()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:405)**

실수 파형의 국소 최댓값을 찾습니다.

- 왼쪽보다 높아지는 지점에서 시작합니다.
- 같은 높이가 이어지면 평평한 꼭대기 전체를 확인합니다.
- 그 뒤에 값이 낮아지면 피크로 인정합니다.
- 평평한 꼭대기는 중앙 인덱스를 사용합니다.
- 배열 양 끝은 피크로 세지 않습니다.

이 단계에서는 피크 사이 거리나 돌출 정도를 검사하지 않습니다.

**29. [`suppress_close_peaks()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:427)**

서로 너무 가까운 피크 중 우선순위가 낮은 것을 제외합니다.

```text
최소 거리 = max(1, nearbyint(fs / max_hz))
```

예를 들어 `fs=100Hz`, `max_hz=1Hz`이면 최소 거리는 100샘플, 약 1초입니다.

높은 피크부터 처리하고 최소 거리보다 가까운 후보에 `keep=0`을 표시합니다.

**30. [`peak_threshold()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:446)**

피크 prominence의 최소 기준을 구합니다.

```text
threshold = 0.3 × 파형의 표준편차
```

분산은 `n`으로 나눕니다. 이 값은 절대 피크 높이가 아니라 주변 대비 돌출 정도에 적용됩니다.

**31. [`peak_prominence()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:460)**

피크가 주변 골짜기에서 얼마나 솟아 있는지 계산합니다.

- 좌우로 이동하며 최솟값을 찾습니다.
- 현재 피크보다 높은 값을 만나면 그 방향 탐색을 멈춥니다.
- 양쪽 최솟값 중 더 높은 값을 기준선으로 사용합니다.

```text
prominence = peak_height - max(left_min, right_min)
```

**32. [`count_peaks()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:484)**

위 피크 함수들을 묶어 시간 파형의 bpm을 구합니다.

```text
국소 피크 찾기
→ 가까운 피크 제거
→ prominence 기준 미달 제거
→ 남은 피크 수와 처음·마지막 위치 확인
```

피크가 2개 이상이면:

```text
bpm = (피크 수 - 1) × fs / (마지막 위치 - 첫 위치) × 60
```

전체 기록 길이로 피크 수를 나누지 않고, **첫 피크와 마지막 피크 사이의 주기 수**를 사용합니다. 피크가 2개 미만이면 bpm은 0입니다.

**33–34. 전체 분석과 채택 판정**

**33. [`analyze()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:713)**

보정·보간된 복소 CSI에서 두 추정치를 만들고 판정합니다.

실제 순서는 다음과 같습니다.

1. 열별 복소 평균을 제거합니다.
2. 대역 에너지로 상위 열을 고릅니다.
3. 열들을 합쳐 실수 파형을 만듭니다.
4. Hampel 필터와 전체 탐색 대역의 Butterworth 필터를 적용합니다.
5. 변동 크기가 `1e-12` 이하이면 `CSI_PHASE_FLAT`으로 보류합니다.
6. 복소 스펙트럼에서 주파수 후보 `f0`를 구합니다.
7. 실수 파형을 다음 대역으로 다시 필터링합니다.

```text
[max(0.7 × f0, min_hz), min(1.5 × f0, max_hz)]
```

8. 그 파형에서 피크를 세어 두 번째 bpm을 구합니다.
9. 두 결과를 비교해 채택 여부를 정합니다.

여기서 중요한 점은 **피크 계수 경로의 두 번째 필터가 스펙트럼 후보에 의존한다는 것**입니다. 두 추정치가 일치해도 완전히 독립된 증거 두 개가 일치한 것은 아닙니다.

`hampel_filter()`와 `butter_bandpass_zero_phase()`의 구현은 이 파일이 아니라 `filters.c`에 있습니다.

**34. [`decide_phase_candidate()`](/home/kang/.workspace/wifi-csi-respiration/src/phase.c:692)**

최종 bpm과 보류 사유를 결정합니다.

기본적으로 최종 bpm은 스펙트럼 추정값입니다. 다만 최대 피크가 탐색 대역의 첫 칸이나 마지막 칸이고 피크 계수 bpm이 존재하면, 최종 표시값을 피크 계수 bpm으로 바꿉니다.

이를 `peak_fallback`으로 기록합니다.

채택 조건은 다음과 같습니다.

```text
sharpness > min_sharpness
abs(spectral_bpm - peak_count_bpm) < max_agreement_bpm
bpm > 0
다른 보류 사유 없음
```

- sharpness 부족: `CSI_PHASE_WEAK`
- 두 추정치 불일치 또는 피크 계수 실패: `CSI_PHASE_DISAGREEMENT`
- 모두 통과: `accepted = 1`

fallback이 발생해도 일치도는 원래 `spectral_bpm`과 `peak_count_bpm` 사이에서 계산합니다. 표시값을 바꿨다고 차이를 0으로 만들지는 않습니다.

현재 코드를 해석할 때 특히 기억할 점은 세 가지입니다.

- **0.005Hz는 탐색 간격입니다.** 실제 관측 시간으로 정해지는 주파수 분해능과 다릅니다.
- **`accepted`는 현재 규칙을 통과한 주기 후보입니다.** 기존 빈 방 데이터에서도 채택이 발생했습니다.
- **전체 윈도우를 메모리에 보관하는 구조입니다.** 현재 그대로 ESP32-C5에서 실시간 실행하도록 메모리를 줄인 구현은 아닙니다.
