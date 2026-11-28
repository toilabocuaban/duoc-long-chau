#!/bin/bash
# Fastest 10-day run: no Gazebo window, same calculations as the 3D run (10 shifts x 8 h = 288000 s).
# The console shows only progress lines; the full event log goes to controller.log.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"
pkill -9 -f "controller.py" 2>/dev/null || true
echo "=========================================================================="
echo " CHẠY NHANH 10 NGÀY (KHÔNG ĐỒ HỌA) - dự kiến khoảng 30-35 phút"
echo " Tiến độ hiện sau mỗi ngày. Báo cáo: reports/Bao_Cao_Moi_Nhat.xlsx"
echo " Không mở file Excel báo cáo cho tới khi thấy dòng [HOÀN THÀNH]."
echo "=========================================================================="
python3 controller.py --fast --no-gz --continuous --step-dt 0.1 --duration 288000 2>&1 \
  | tee controller.log \
  | grep --line-buffered -E '\[TIẾN ĐỘ\]|\[HOÀN THÀNH\]|\[CẢNH BÁO\]|Traceback|Error|Excel report saved'
status=${PIPESTATUS[0]}
if [[ $status -eq 0 ]]; then
  echo "Xong. Mở báo cáo: reports/Bao_Cao_Moi_Nhat.xlsx (sheet đầu: Bao_Cao_Theo_Ngay)"
else
  echo "Mô phỏng dừng với lỗi (mã $status). Xem chi tiết trong controller.log"
fi
exit $status
