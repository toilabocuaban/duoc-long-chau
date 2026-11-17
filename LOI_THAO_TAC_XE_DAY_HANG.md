# Báo Cáo Phân Tích & Khắc Phục Lỗi Thao Tác Xe Đẩy Hàng (Xe Nâng Tay Pallet)

> **Ngày thực hiện:** 12/11/2026  
> **Tài liệu tham chiếu:** [`dock_flow.py`](file:///d:/work1/duoc-long-chau/dock_flow.py), [`build_world.py`](file:///d:/work1/duoc-long-chau/build_world.py), hình ảnh phản ánh hiện trạng từ người dùng (`media_1794458448737_9a83b486.png`).

---

## 1. Hiện trạng và Vấn đề được phát hiện

Qua hình ảnh chụp thực tế tại khu vực cửa bốc dỡ hàng (Dock Door) và đối chiếu với mã nguồn mô phỏng trước khi sửa đổi, các thao tác của công nhân khi sử dụng xe đẩy / xe nâng tay hàng (`dock_jack`) gặp các lỗi nghiêm trọng sau khiến mô phỏng nhìn rất thiếu thực tế:

1. **Xe nâng tay đỗ lệch hướng 90 độ tại cửa Dock:**
   - Trong `scene()` và `initialize()` cũ, xe nâng tay `dock_jack_{i}` được khởi tạo với góc quay `yaw = 0` (hướng Đông - Tây).
   - Do đó, càng xe đâm sang bên phải (+X), thân và tay cầm chìa sang bên trái (-X), đỗ ngang chắn cửa ra vào thay vì đút dọc theo chiều bốc xếp xe tải (trục Bắc - Nam, trục Y).
   - Tay cầm bị áp sát vào vách kính/cột bên trái, xa rời vị trí đứng làm việc của công nhân.

2. **Hiện tượng "dịch chuyển tức thời" (Teleportation):**
   - Sau khi đặt kiện hàng cuối cùng lên pallet, công nhân không hề có cử chỉ bước chân tới cần lái của xe nâng.
   - Khi tiến trình `transfer()` bắt đầu, tọa độ công nhân ngay lập tức nhảy tức thời từ điểm đặt hàng sang điểm di chuyển.

3. **Hoàn toàn thiếu thao tác kích nâng thủy lực (Hydraulic Pumping):**
   - Ngoài thực tế, kiện hàng và pallet khi xếp dỡ nằm cố định trên mặt sàn kho (`z = 0`). Để di chuyển xe nâng tay, công nhân bắt buộc phải đứng trước cần lái, gạt van kích nâng và dập cần lái 2–3 nhịp để kích bơm dầu thủy lực, nhấc pallet lên cao khoảng 5 cm khỏi mặt sàn nhằm tránh ma sát.
   - Trong mô hình cũ, xe trượt thẳng trên mặt đất ở cao độ `z = 0` mà không hề có thao tác kích nâng.

4. **Góc quay của xe nâng bị khóa cứng (`yaw = 0`):**
   - Trong hàm `transfer()` cũ:
     ```python
     p = tuple(a[k] + (b[k] - a[k]) * q for k in range(3)) + (0,)
     out = {jack: p, ...}
     ```
   - Góc xoay `yaw` của xe luôn bằng `0` trong suốt toàn bộ lộ trình (kể cả khi đi thẳng về phía Bắc, rẽ vào hành lang Đông/Tây, hay quay về). Xe nâng trôi trượt ngang (drift/slide sideways) như con cua thay vì xoay đầu xe theo hướng chuyển động.

5. **Công nhân không cầm vào tay lái (Disconnection between worker and handle):**
   - Vị trí công nhân trong mã nguồn cũ được tính:
     ```python
     wx = p[0] - .95 * math.cos(yaw)
     wy = p[1] - .95 * math.sin(yaw)
     ```
   - Khi di chuyển lên phía Bắc (`yaw = pi/2`), công nhân đứng ở phía Nam pallet (`wy = p[1] - 0.95`), nhưng tay cầm xe nâng lại nằm ở phía Tây (`x = p[0] - 0.78`).
   - Khoảng cách giữa tay công nhân và tay cầm xe lên tới hơn 1.2 mét. Tay công nhân giơ ra giữa không trung, hoàn toàn không chạm vào xe.

6. **Thiếu thao tác hạ pallet (Lowering) tại đích đến:**
   - Khi xe đến trạm tiếp nhận của AGV hoặc trạm xả hàng rời, pallet lập tức biến mất hoặc chuyển giao trạng thái mà không có thao tác xả van thủy lực để hạ pallet tiếp đất an toàn.

7. **Thiếu tính chân thực khi đẩy xe không tải quay về:**
   - Khi quay về, xe nâng tay không tải tiếp tục bị trượt giật lùi hoặc trượt ngang, không thể hiện được tư thế đẩy xe (pushing) tự nhiên của người lao động.

---

## 2. Giải Pháp và Chi Tiết Khắc Phục Kỹ Thuật

Chúng tôi đã thiết kế lại toàn bộ động học thao tác xe nâng tay trong [`dock_flow.py`](file:///d:/work1/duoc-long-chau/dock_flow.py) nhằm phản ánh đúng 100% quy trình làm việc thực tế:

### A. Tinh chỉnh ngoại quan mô hình xe nâng tay (`dock_jack`)
- Bổ sung 2 bánh lái xoay đôi phía sau (`steer_l`, `steer_r`) bên dưới cụm thân thủy lực.
- Bổ sung 2 con lăn chịu tải trước đầu càng nâng (`roller_l`, `roller_r`).
- Chuẩn hóa chiều cao tay nắm lái (`grip` ở `z = 1.18m`, bề rộng `0.50m`) khớp chính xác với khoảng cách hai bàn tay của công nhân (`y = ±0.25m`).

### B. Chuẩn hóa góc đỗ ban đầu tại cửa xuất nhập
- Xe nâng tay tại vị trí `stage(d)` được gán góc ban đầu `yaw = -math.pi / 2` (-90 độ):
  - Càng nâng hướng về phía Nam (-Y), luồn chuẩn xác vào 2 khe hở của pallet gỗ/nhựa.
  - Thân xe và tay cầm hướng về phía Bắc (+Y), quay thẳng vào lối đi nội bộ của nhà kho, đúng vị trí thuận tiện nhất để công nhân tiếp cận.

### C. Bổ sung chuỗi động tác tiếp cận & Kích nâng thủy lực (`pump`)
- Sau khi đóng kiện hàng cuối cùng, công nhân bước từ từ đến trước cần lái xe (`hx, hy + 0.28m, yaw = -math.pi / 2`).
- Công nhân thực hiện 2 nhịp dập cần kích nâng:
  - **Nhịp 1:** Cúi người (`bend = 0.44`, `arm = 0.72`) ấn tay cần xuống, nhấc xe và pallet lên cao `+0.025m`; sau đó đứng thẳng lại đón tay cần.
  - **Nhịp 2:** Cúi người ấn tay cần lần hai, nhấc xe, pallet và toàn bộ 18 thùng thuốc lên cao độ chuẩn di chuyển `z = +0.05m`.
- Công nhân đứng thẳng người, chuyển thế tay sang tư thế kéo xe.

### D. Chuẩn hóa động học kéo hàng có tải (Pulling Kinematics)
- Hướng di chuyển vector trên từng đoạn đường: $\theta = \text{atan2}(\Delta y, \Delta x)$.
- Góc xe nâng tự động xoay theo phương di chuyển: $\text{yaw\_jack} = \theta + \pi$ (tay lái dẫn đầu, càng nâng và pallet theo sau).
- Pallet và các kiện hàng xoay đồng trục với càng nâng: $\text{yaw\_deck} = \text{yaw\_jack} + \pi/2$.
- Công nhân đi trước hướng di chuyển:
  $$w_x = p_x + 1.06 \cos\theta, \quad w_y = p_y + 1.06 \sin\theta, \quad \text{yaw} = \theta$$
- Hai tay công nhân với về sau góc âm (`arm = -0.45`, `bend = 0.05`), bàn tay đặt **chuẩn xác từng milimet** lên thanh ngang `grip` của tay lái xe nâng.
- Hai chân công nhân vung bước chân thực (`swing`) theo nhịp chuyển động $q$.

### E. Thao tác xả van hạ pallet tại trạm tiếp nhận (`lower`)
- Khi xe đến vị trí giao hàng (trạm AGV hoặc trạm Zone 05), công nhân dừng bước.
- Công nhân bóp cò van xả thủy lực trên tay cầm; xe, pallet và các thùng hàng hạ êm từ cao độ `+0.05m` tiếp đất vững chãi tại `z = 0.0m`.

### F. Động học đẩy xe không tải quay về Dock (Empty Return Push)
- Khi quay về, công nhân đi sau xe nâng, đẩy tay cầm về phía trước:
  - Góc xe nâng: $\text{yaw\_jack} = \theta$.
  - Công nhân đi sau: $w = p - 1.10(\cos\theta, \sin\theta)$, mặt nhìn thẳng hướng đi.
  - Hai tay vươn về trước (`arm = 0.65`, `bend = 0.05`) nắm chặt tay lái đẩy xe đi tới.
- Khi về đến cửa Dock, càng xe trượt thẳng vào vị trí `stage(d)` hướng về phía xe tải, công nhân buông tay lái và bước về vị trí chuẩn bị đón chuyến hàng tiếp theo.

---

## 3. Kết Quả Kiểm Tra & Xác Thực Kỹ Thuật

- **Biên dịch mô hình thế giới:** Đã chạy `python3 build_world.py`, cập nhật toàn diện file mô hình [`warehouse.sdf`](file:///d:/work1/duoc-long-chau/warehouse.sdf) (2271 models).
- **Kiểm thử logic & hình học:**
  - Chạy toàn bộ 44 unit tests của hệ thống (`python3 -m unittest discover ...`): **44/44 bài test PASS 100%**.
  - Kiểm thử riêng biệt luồng Dock (`test_dock_flow.py`): **3/3 test PASS 100%**.
  - Kiểm thử chu trình tích hợp xe nâng tay không tải / có tải: Hoàn thành trơn tru không lỗi.
- **Tuân thủ chỉ thị người dùng:**
  - **Không chỉnh sửa Layout và Logic điều phối hệ thống**: Các tọa độ dock, agv lane, conveyor, khu vực lưu kho đều được bảo toàn nguyên vẹn.
  - **Không tự ý khởi chạy Gazebo / mô phỏng**: Hoàn thành toàn bộ mã nguồn và dừng lại để người dùng tự khởi chạy và nghiệm thu thực tế.

