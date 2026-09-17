#pragma once

#include <stdint.h>

/** 패킷당 raw CSI 버퍼 최대 바이트 수. 실제 유효 길이는 csi_record_t::len. */
#define CSI_BUF_MAX 512
/** 최대 서브캐리어 수. buf가 [im, real] 2바이트 쌍이라 CSI_BUF_MAX / 2. */
#define CSI_MAX_SUBCARRIERS (CSI_BUF_MAX / 2)

/**
 * RX CSI 콜백이 wifi_csi_info_t에서 복사해 큐에 넣는 패킷 레코드.
 * ESP-IDF 타입에 의존하지 않으므로 보드와 PC 테스트에서 같이 쓴다.
 *
 * buf 배치 (k번째 서브캐리어):
 *   buf[2k]   = im (int8)
 *   buf[2k+1] = real (int8)
 */
typedef struct {
  uint32_t seq;       /**< TX 페이로드(offset 15)의 송신 카운터 */
  uint32_t timestamp; /**< rx_ctrl.timestamp, 하드웨어 수신 시각 [us], uint32라
                         약 71.6분마다 wrap */
  uint32_t dropped;   /**< 큐가 가득 차서 버려진 레코드 누적 수 */
  float compensate_gain;      /**< 게인 보정 계수 (esp_csi_gain_ctrl). buf에는
                                 적용되지 않음 */
  int8_t rssi;                /**< rx_ctrl.rssi */
  int8_t noise_floor;         /**< rx_ctrl.noise_floor */
  int8_t fft_gain;            /**< 수신 FFT 게인 (esp_csi_gain_ctrl) */
  uint8_t agc_gain;           /**< 수신 AGC 게인 (esp_csi_gain_ctrl) */
  uint8_t channel;            /**< rx_ctrl.channel, 주 채널 번호 */
  uint8_t bb_format;          /**< rx_ctrl.cur_bb_format, 수신 PHY 포맷 */
  uint8_t first_word_invalid; /**< info->first_word_invalid, 1이면 buf 앞
                                 4바이트가 무효 */
  uint16_t sig_len;           /**< rx_ctrl.sig_len, 수신 패킷 길이 */
  uint16_t len;               /**< buf의 유효 바이트 수 (<= CSI_BUF_MAX) */
  int8_t
      buf[CSI_BUF_MAX]; /**< raw CSI, 게인 보정 전. 배치는 구조체 설명 참고 */
} csi_record_t;

/** 전처리 결과에 남기는 패킷 메타데이터 (csi_record_t에서 복사). */
typedef struct {
  uint32_t seq;       /**< TX 송신 카운터 (csi_record_t::seq) */
  uint32_t timestamp; /**< 하드웨어 수신 시각 [us] (csi_record_t::timestamp) */
  uint32_t dropped;   /**< 큐 오버플로 누적 수 (csi_record_t::dropped) */
  float compensate_gain; /**< 게인 보정 계수 (csi_record_t::compensate_gain) */
  int8_t rssi;           /**< csi_record_t::rssi */
  int8_t noise_floor;    /**< csi_record_t::noise_floor */
} csi_meta_t;

/**
 * 한 패킷의 전처리 결과: 메타데이터 + 서브캐리어별 복소 CSI.
 * k번째 서브캐리어 = re[k] + j * im[k], 유효 범위는 0 <= k < n.
 * 크기가 약 2KB라 보드에서는 태스크 스택 대신 static 등에 둔다.
 */
typedef struct {
  csi_meta_t meta; /**< 패킷 메타데이터 */
  uint16_t n;      /**< 유효 서브캐리어 수 (= csi_record_t::len / 2) */
  float re[CSI_MAX_SUBCARRIERS]; /**< 실수부 (buf 홀수 인덱스) */
  float im[CSI_MAX_SUBCARRIERS]; /**< 허수부 (buf 짝수 인덱스) */
  uint8_t valid[CSI_MAX_SUBCARRIERS];
} csi_frame_t;

/** csi_preprocess() 결과. OK가 아니면 그 패킷은 건너뛴다. */
typedef enum {
  CSI_PRE_OK = 0,             /**< 성공, out이 채워짐 */
  CSI_PRE_ERR_LEN_ODD,        /**< len이 홀수 */
  CSI_PRE_ERR_LEN_RANGE,      /**< len == 0 또는 len > CSI_BUF_MAX */
  CSI_PRE_ERR_GAIN_NONFINITE, /**< compensate_gain이 nan/inf */
} csi_pre_status_t;
