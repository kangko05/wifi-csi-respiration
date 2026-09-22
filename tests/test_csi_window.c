/* Host check: cc -std=c11 -Wall -Wextra -Werror -Iinclude -Icsi-rx/main
 * tests/test_csi_window.c csi-rx/main/csi_window.c -o /tmp/test_csi_window
 * /tmp/test_csi_window */
#include <assert.h>
#include <stdio.h>

#include "csi_window.h"

int main(void) {
    csi_record_t storage[3];
    csi_window_t window;
    assert(!csi_window_init(NULL, storage, 3, 60));
    assert(!csi_window_init(&window, NULL, 3, 60));
    assert(!csi_window_init(&window, storage, 0, 60));
    assert(!csi_window_init(&window, storage, 3, 0));
    assert(!csi_window_init(&window, storage, 3, UINT32_MAX));
    assert(csi_window_init(&window, storage, 3, 60));

    csi_record_t record = {.timestamp = 100, .len = 2, .buf = {-3, 4}};
    assert(csi_window_push(&window, &record));
    record.buf[0] = 42;
    assert(csi_window_at(&window, 0)->buf[0] == -3);
    record.timestamp = 120;
    assert(csi_window_push(&window, &record));
    record.timestamp = 140;
    assert(csi_window_push(&window, &record));
    record.timestamp = 150;
    assert(csi_window_push(&window, &record));
    assert(window.count == 3 && window.capacity_drops == 1);
    assert(csi_window_at(&window, 0)->timestamp == 120);
    assert(csi_window_at(&window, 2)->timestamp == 150);
    assert(csi_window_at(&window, 3) == NULL);
    record.timestamp = 180;
    assert(csi_window_push(&window, &record));
    assert(window.count == 3 && window.capacity_drops == 1);
    assert(csi_window_at(&window, 0)->timestamp == 140);
    record.timestamp = 179;
    assert(!csi_window_push(&window, &record));
    record.timestamp = 200;
    record.len = 1;
    assert(!csi_window_push(&window, &record));
    record.len = CSI_BUF_MAX + 2;
    assert(!csi_window_push(&window, &record));
    record.len = 2;
    csi_window_expire(&window, 240);
    assert(window.count == 0);

    record.timestamp = UINT32_MAX - 20;
    assert(csi_window_push(&window, &record));
    record.timestamp = 10;
    assert(csi_window_push(&window, &record));
    assert(window.count == 2);
    csi_window_expire(&window, 39);
    assert(window.count == 1 && csi_window_at(&window, 0)->timestamp == 10);
    record.timestamp = 500;
    assert(csi_window_push(&window, &record));
    assert(window.count == 1 && window.capacity_drops == 1);

    puts("PASS: ordering, copied ownership, capacity, time expiry, gaps, wrap, invalid input");
    return 0;
}
