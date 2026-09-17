#pragma once

#include <stddef.h>
#include <stdint.h>

#include "csi_record.h"

/** 보간 격자 간격 [us] (100Hz) */
#define CSI_INTERP_STEP_US 10000
/** 칸별 유효 샘플 최대 간격 [us], 창 시작/끝까지 포함 */
#define CSI_INTERP_MAX_GAP_US 100000
/** 칸별 유효 샘플 비율 하한 */
#define CSI_INTERP_MIN_FRACTION 0.9f
/** 칸별 유효 샘플 수 하한 */
#define CSI_INTERP_MIN_PACKETS 2

/**
 * 창 하나(프레임 배열)의 칸별 진폭을 100Hz 균일 격자로 선형 보간한다.
 *
 * - 시각: frames[i].meta.timestamp - frames[0].meta.timestamp (uint32 wrap 1회
 * 처리)
 * - 칸 선택: 유효 샘플 수, 유효 비율, 최대 간격 조건을 통과한 칸만 eligible
 * - 격자 범위: eligible 칸들의 첫 유효 시각 중 최댓값 ~ 마지막 유효 시각 중
 * 최솟값
 * - eligible이 아닌 칸의 출력은 0
 *
 * @param frames   csi_preprocess() 결과 배열 (시간순, 칸 수 동일)
 * @param n_frames 프레임 수 (>= 2)
 * @param out      출력 진폭, out[g * n + k] (n = frames[0].n). 크기 max_grid *
 * n
 * @param eligible 칸별 사용 여부 출력 (크기 n)
 * @param max_grid out이 담을 수 있는 격자 점 수
 * @param start_us 첫 격자 시각 출력 [us, frames[0] 기준]
 * @return 격자 점 수. 실패하면 0
 */
size_t csi_amp_interp(const csi_frame_t *frames, size_t n_frames, float *out,
                      uint8_t *eligible, size_t max_grid, uint32_t *start_us);
