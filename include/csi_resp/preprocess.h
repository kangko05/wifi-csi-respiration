#pragma once

#include "csi_record.h"

/**
 * csi_record_t 한 개를 메타데이터 + 복소 CSI(csi_frame_t)로 변환한다.
 *
 * - 검사: len 범위(1..CSI_BUF_MAX), len 짝수, compensate_gain 유한값
 * - 변환: buf[2k] -> im[k], buf[2k+1] -> re[k], n = len / 2
 * - 게인 보정은 적용하지 않는다 (계수만 meta에 남김)
 *
 * @param rec 입력 레코드 (읽기 전용)
 * @param out 결과를 쓸 곳. 실패하면 건드리지 않는다
 * @return CSI_PRE_OK 또는 실패 사유
 */
csi_pre_status_t csi_preprocess(const csi_record_t *rec, csi_frame_t *out);
