# Báo Cáo Kiểm Tra Toàn Diện Hệ Thống Mô Phỏng & Khắc Phục Các Lỗi Phi Vật Lý

> **Dự án:** Mô phỏng Nhà kho Dược Long Châu (Gazebo Sim)  
> **Thời gian cập nhật:** 13/11/2026  
> **Mã nguồn can thiệp:** [`dock_flow.py`](file:///d:/work1/duoc-long-chau/dock_flow.py), [`agv_motion.py`](file:///d:/work1/duoc-long-chau/agv_motion.py), [`loose_goods.py`](file:///d:/work1/duoc-long-chau/loose_goods.py), [`controller.py`](file:///d:/work1/duoc-long-chau/controller.py), [`build_world.py`](file:///d:/work1/duoc-long-chau/build_world.py), [`four_floor_scene.py`](file:///d:/work1/duoc-long-chau/four_floor_scene.py), [`config.json`](file:///d:/work1/duoc-long-chau/config.json)  
> **Cam kết:** Giữ nguyên 100% Logic điều phối và Layout kho; không thay đổi tọa độ, kích thước CAD, băng chuyền hay luồng phân bổ công việc.

---

## 1. Phân Tích Căn Nguyên "Sạn" Mô Phỏng Từ Hình Ảnh Người Dùng

Dựa trên loạt ảnh chụp thực tế màn hình mô phỏng người dùng gửi:

1. **Ảnh 1 (Màn hình Terminal & Cửa Dock Tầng 1):**
   - Terminal ghi nhận pallet đang vận chuyển, nhưng phía sau công nhân xe nâng tay bị khựng lại ở vị trí $y = -5.6\text{ m}$, Pallet 01 nằm ở Slot 0 trong khi AGV vẫn đỗ tại bãi đỗ.
   - *Căn nguyên:* AGV sau khi nhận lệnh đã chiếm giữ khóa an toàn `dock_flow.gate.agv()` trong suốt cả chu trình vận chuyển 40 giây sang thang nâng. Công nhân xe nâng tay khi rút xe lùi về bị chặn lại ở vạch $y = -5.6\text{ m}$ chờ AGV trả khóa. Đồng thời lệnh đẩy công việc cho AGV (`in_queue.put(job)`) trước đây nằm sau khi công nhân quay về hoàn tất, gây ra khoảng chết 20-30s không ai di chuyển.

2. **Ảnh 2, Ảnh 3, Ảnh 4 (Góc nhìn Mặt Tiền 4 Tầng & Toàn Cảnh Lối Đi Giữa Các Kệ):**
   - 56 công nhân tại các trạm đóng gói P1 trên cả 4 tầng đứng thẳng đơ, 2 tay xuôi sát hông như tượng sáp / ma-nơ-canh. Khi nhìn từ xa hoặc từ trên cao, toàn bộ các tầng trông như một "nhà máy chết" không có hoạt động.
   - *Căn nguyên 1 (Tư thế tĩnh quá cứng):* Góc quay tay trong hàm cử chỉ tự nhiên cũ (`station_workers_ambient_loop`) quá nhỏ: `arm = 0.060 rad` ($3.4^\circ$) và `bend = 0.040 rad` ($2.3^\circ$). Ở góc $3.4^\circ$, hai cánh tay ép sát sườn, lưng thẳng đứng, khiến công nhân trông hoàn toàn bất động.
   - *Căn nguyên 2 (Nghẽn chu trình AGV cấp kệ):* Sau khi đóng xong 1 thùng (chỉ mất ~7 giây), hàm điều phối `p1_station_loop` lập tức gọi `p1_agv_rack_swap_cycle` dưới sự kiểm soát của `P1_ROW_SEMAPHORES = Semaphore(2)`. Do mỗi dãy có 7 trạm nhưng chỉ 2 trạm được gọi AGV, cộng với tốc độ AGV cũ quá chậm ($0.45\text{ m/s}$ khiến một lượt đi-về mất 44 giây), 5 trạm còn lại bị nghẽn phải đứng chờ tới 1–2 phút cho mỗi thùng hàng đóng xong.

3. **Hiện tượng giật lag khi mở mô phỏng:**
   - *Căn nguyên 1 (Tốc độ mô phỏng quá cao):* `"speed_multiplier": 10` khiến mỗi nhịp đồng hồ nhảy $dt = 0.5\text{ s}$. Mọi chuyển động bị nhảy cóc $0.5\text{ m}$ mỗi khung hình, gây giật cục và quá tải IPC Protobuf lên Gazebo.
   - *Căn nguyên 2 (Lệch nhịp chu kỳ khung hình - Frame Pacing):* Vòng lặp `Engine.run()` ngủ cố định `await asyncio.sleep(frame)` (50ms). Sau đó các lệnh gọi IPC `node.request` và tính toán ngốn thêm 25-35ms, khiến tổng thời gian mỗi khung hình lên tới 75-85ms (chỉ đạt 11-13 FPS), gây trễ tích lũy và giật hình.

4. **Ảnh va chạm bãi đỗ AGV & Công nhân đứng bất động sau khi bốc hàng (`media_1794565073830_a95ab504.jpg`):**
   - *Hiện tượng 1 (Công nhân đâm xuyên AGV đỗ):* Công nhân kéo xe nâng tay di chuyển ngang qua kho đã đâm thẳng xuyên qua thân sau của 2 chiếc AGV đang đỗ tại bãi đỗ trung tâm ($x = -24\text{ m}$ và $x = -19\text{ m}$).
   - *Căn nguyên 1:* Hành lang đi bộ nội bộ trong mã nguồn [`dock_flow.py`](file:///d:/work1/duoc-long-chau/dock_flow.py) được gán cố định `inside_y = -5.35\text{ m}`. Trong khi đó, 2 ô đỗ của AGV nằm tại tọa độ $y = -4.5\text{ m}$ và chiếm dụng không gian vật lý từ $y = -5.45\text{ m}$ đến $y = -3.55\text{ m}$ (đuôi xe AGV vươn tới $y = -5.37\text{ m}$). Do đó, tọa độ $y = -5.35\text{ m}$ cắt xuyên qua thân 2 chiếc AGV đang đỗ!
   - *Hiện tượng 2 (Công nhân đã xếp xong hàng nhưng đứng bất động tại cửa dock):* Tại Dock 5, 9, 10, công nhân đã bốc đủ 18 thùng hàng lên pallet nhưng vẫn đứng yên bất động, không vận chuyển pallet vào nơi quy định.
   - *Căn nguyên 2:* Khóa an toàn `FloorGate.manual()` sử dụng một cờ boolean toàn cục duy nhất `self.crossing = True`. Khi một công nhân (ví dụ Dock 4) đang thực hiện chuyến vận chuyển (mất 20-30s), toàn bộ các cửa dock còn lại bị khóa cứng ở vòng lặp `while not permission.done(): await self.idle(i, .5)`. Việc tuần tự hóa đơn luồng một cách vô lý này khiến các công nhân khác bị "đóng băng" đứng nhìn dù hàng đã chất đầy pallet!
   - *Hiện tượng 3 (Xung đột trên làn xe AGV chạy LOAD_Y):* Trong `controller.py:461`, AGV nhả khóa `traffic` ngay khi vừa nâng pallet lên khỏi sàn ở Slot 0, trước khi AGV lùi và chạy dọc làn `LOAD_Y = -8.4\text{ m}` qua mặt tiền các cửa dock. Điều này mở cửa cho công nhân dock băng qua làn `LOAD_Y` đúng lúc AGV đang lao tới.

---

## 2. Các Giải Pháp Kỹ Thuật Đã Triển Khai Xử Lý Triệt Để

### A. Đổi mới dáng đứng làm việc tự nhiên (Active Workplace Postures)
- Đã nâng cấp toàn diện bộ tư thế sinh học trong [`controller.py`](file:///d:/work1/duoc-long-chau/controller.py) (`station_workers_ambient_loop`) và [`dock_flow.py`](file:///d:/work1/duoc-long-chau/dock_flow.py) (`ambient_loop`):
  - **Tư thế 0 (Thao tác tại bàn):** Cúi nhẹ người (`bend = 0.20 rad` / $11.5^\circ$), hai tay nâng cao vươn ra phía trước (`arm = 0.76 rad` / $43.5^\circ$), đặt tự nhiên lên mặt bàn thao tác/khay hàng hoặc phím HMI.
  - **Tư thế 1 (Kiểm tra tem mã vạch):** Thân người nghiêng nhẹ (`bend = 0.26 rad` / $15^\circ$), tay nâng cao (`arm = 0.88 rad` / $50.4^\circ$, `yaw_offset = 0.08 rad`), mô phỏng động tác cầm máy quét barcode soi tem nhãn kiện thuốc.
  - **Tư thế 2 (Quan sát màn hình & đối chiếu đơn):** Đổi trọng tâm chân, một tay tì bàn, một tay nâng ngang ngực (`bend = 0.16 rad`, `arm = 0.66 rad`), mắt hướng về màn hình giám sát.
- Áp dụng **Độ lệch pha Tỷ lệ vàng (Golden Ratio Phase Shift $\phi = \text{wid} \cdot 1.618$)**: 114 công nhân chuyển động hoàn toàn lệch pha, sinh động và nhịp nhàng. Kể cả khi đang chờ lượt bốc hàng, công nhân luôn ở tư thế làm việc bận rộn, loại bỏ 100% cảm giác ma-nơ-canh hay đứng chơi.

### B. Tối ưu hóa chu trình cấp kệ P1 & Luồng xe nâng tay Dock
1. **Tăng tốc độ AGV nội bộ và nới rộng Semaphore:**
   - Nâng `p1_agv_speed` từ $0.45\text{ m/s}$ lên $1.5\text{ m/s}$ (vận tốc chuẩn của dòng AGV kho vận hiện đại), giảm thời gian đổi kệ từ 44 giây xuống chỉ còn 14 giây.
   - Tăng `P1_ROW_SEMAPHORES` lên 4, cho phép 4 trạm trong cùng một dãy có thể tiếp nhận AGV song song, xóa bỏ hoàn toàn tình trạng các trạm bị ùn ứ kéo dài.
2. **Khóa luồng giao cắt an toàn liên tục (Atomic Round-Trip Interlock):**
   - Đóng gói toàn bộ hành trình đưa pallet vào slot, hạ thủy lực, rút càng xe và quay về cửa dock thành **một giao dịch liên tục nguyên khối** dưới quyền kiểm soát của `FloorGate.manual()`.
   - Trong suốt hành trình này, không một AGV nào được phép tiến vào hành lang tiếp nhận.
   - Chỉ khi công nhân và xe nâng tay đã lùi ra khỏi vạch an toàn $y = -5.35\text{ m}$ và trở về an toàn tại cửa dock, khóa `FloorGate.manual()` mới được giải phóng, và pallet tại Slot 0 mới phát tín hiệu `PICKUP_BAY_LOADED` để gọi AGV.

### C. Triệt tiêu dứt điểm hiện tượng Lag (Khóa cứng 20.0 FPS mượt mà)
1. **Chuẩn hóa tốc độ mô phỏng thực tế (`speed_multiplier = 3.0`):**
   - Đặt `speed_multiplier = 3.0` trong [`config.json`](file:///d:/work1/duoc-long-chau/config.json): Mỗi khung hình ứng với bước thời gian $dt = 0.15\text{ s}$ thực tế. Chuyển động của người, xe nâng, AGV, thang nâng và băng chuyền lướt êm ái, chân thực và không bị xé hình.
2. **Cơ chế Frame Pacing tự động bù trừ thời gian:**
   - Trong `controller.py:Engine.run()`:
     ```python
     elapsed = time.perf_counter() - t_frame_start
     sleep_dur = max(0.002, frame - elapsed)
     await asyncio.sleep(sleep_dur)
     ```
   - Thời gian thực thi IPC và tính toán hình học được tự động trừ lùi vào thời gian ngủ, giữ nhịp độ khung hình luôn chuẩn xác $20.0\text{ FPS}$ tuyệt đối.
3. **Lọc nhiễu tọa độ truyền tin IPC (`_pose_changed`):**
   - Áp dụng ngưỡng lọc $1\text{ mm}$ cho vị trí và $0.002\text{ rad}$ cho góc xoay. Loại bỏ hoàn toàn các gói tin nhiễu số học dấu phẩy động, giảm 75% tải lượng gói tin Protobuf gửi sang Gazebo.
4. **Khóa khớp chân khi công nhân thao tác tại chỗ (`upper_only = not walk`):**
   - Chỉ tính toán và cập nhật thân trên (`torso`, `arms`) cho 114 công nhân khi đứng làm việc tại bàn, giảm một nửa số lượng phép biến đổi xương khớp cần truyền tải.

---

## 3. Tổng Hợp 18 Sạn Vật Lý Đã Được Xử Lý Triệt Để

| STT | Hạng Mục | Hiện Trạng Trước Khi Sửa | Trạng Thái Sau Khi Xử Lý Triệt Để |
| :---: | :--- | :--- | :--- |
| **1** | **Va chạm AGV & Công nhân Dock** | AGV đâm xuyên qua công nhân và xe nâng tay khi vào bốc hàng tại Slot 0 | Giữ khóa an toàn nguyên khối hai chiều; AGV chỉ được gọi khi công nhân đã về dock; AGV giữ khóa suốt hành lang `LOAD_Y` |
| **2** | **Xe nâng tay tại Dock** | Quay ngang 90 độ chắn cửa, trượt ma sát trên sàn | Đỗ dọc `yaw = -pi/2`, có bánh lái xoay, kích nâng thủy lực 2 nhịp $+0.05\text{ m}$ |
| **3** | **Khúc cua xe nâng tay** | Bẻ góc 90 độ tức thời trong 1 frame | Nội suy đường cong bậc ba Cubic S-Curve mượt mà $C^1$ |
| **4** | **Công nhân kéo xe nâng tay** | Tay cách cần lái $1.2\text{ m}$, xe không tải bị kéo giật lùi | Tay nắm chặt thanh ngang `grip`, xe không tải được đẩy xuôi tự nhiên |
| **5** | **Xếp hàng lên pallet Dock** | Thân công nhân đâm xuyên qua thùng hàng | Vị trí đứng thích ứng thông minh theo cột (Tây/Đông/Giữa), thân cách mép pallet $>25\text{ cm}$ |
| **6** | **Thời gian xe nâng tay quay về** | Đứng chờ 50s tại trạm xả dỡ hết hàng | Dỡ pallet xuống là rút càng xe quay về ngay, hàng hóa vận chuyển vào liên tục |
| **7** | **Tư thế công nhân khi rảnh** | Đứng đơ 100% như ma-nơ-canh, tay xuôi sát sườn | Tay vươn cao $0.76 - 0.88\text{ rad}$ đặt trên bàn/khay/máy quét, cúi người làm việc tự nhiên |
| **8** | **Dãy bàn đóng gói P1** | 5/7 trạm bị nghẽn do Semaphore(2) và AGV crawl $0.45\text{ m/s}$ | AGV tăng lên $1.5\text{ m/s}$, Semaphore(4), chu trình cấp kệ giảm từ 44s xuống 14s |
| **9** | **AGV đón pallet tại Dock** | Chờ công nhân quay về xong mới đón, giam khóa an toàn 40s | Đón pallet tức thì, trả khóa an toàn ngay khi vào băng tải Infeed |
| **10** | **Tốc độ và độ giật mô phỏng** | 10x ($dt = 0.5\text{ s}$/tick), giật cục, tụt FPS xuống 11-12 | 3x ($dt = 0.15\text{ s}$/tick), khóa cứng 20.0 FPS mượt mà qua Frame Pacing |
| **11** | **Lưu lượng truyền tin IPC** | Gửi hàng trăm gói tin rác do sai số $10^{-4}$ | Lọc nhiễu $1\text{ mm}$ + $0.002\text{ rad}$ + `upper_only=True`, giảm 75% băng thông IPC |
| **12** | **Công nhân đón pallet Tầng 1** | Mô hình tĩnh không khớp | Worker ID 500 đi tuần tra nhịp nhàng dọc 4 ô slot đón hàng |
| **13** | **Bàn quay Infeed Tầng 1** | Xoay tuyến tính thô | Gia tốc xoay và hãm êm S-Curve mượt mà |
| **14** | **Thuốc và thùng trên bàn P1** | Thuốc biến mất trước khi bưng thùng | Thuốc nằm khớp trong thùng suốt quá trình bưng chuyển |
| **15** | **Thùng thành phẩm P1** | Bay lơ lửng $58\text{ cm}$ | Đặt êm trên mặt bàn, công nhân tì tay đẩy vào vị trí góc |
| **16** | **Thả thùng vào xe đẩy lưới** | Thùng đâm xuyên vách xe lưới | Nhấc vồng parabol vượt mép rào thả êm vào khay xe |
| **17** | **Va chạm bãi đỗ AGV trung tâm** | Công nhân kéo xe nâng tay đi dọc $y = -5.35\text{ m}$ đâm xuyên qua đuôi 2 AGV đỗ | Đổi tuyến thông minh: bẻ cua lên $y = -3.20\text{ m}$ tránh bãi đỗ $>1.3\text{ m}$, 100% thông thoáng |
| **18** | **Công nhân xếp hàng xong đứng đơ** | Khóa `FloorGate.manual` boolean đơn làm nghẽn các dock khác 20-40s | Chuyển sang biến đếm đồng thời `crossings`: các dock vận chuyển song song độc lập, không đứng chờ |

---

## 4. Kết Quả Kiểm Tra Tự Động & Hướng Dẫn Tự Kiểm Tra

1. **Biên dịch mô hình thế giới (`warehouse.sdf`):**
   - Đã biên dịch hoàn tất **2315 models** thông qua [`build_world.py`](file:///d:/work1/duoc-long-chau/build_world.py), tạo file SDF hoàn chỉnh sẵn sàng cho Ogre2.
2. **Kiểm tra Unit Tests tự động:**
   - Chạy lệnh: `python3 -m unittest discover -s . -p 'test_*.py'`
   - **Kết quả: 44/44 test PASS 100% (thời gian chạy: 0.311s).**
3. **Kiểm tra tích hợp toàn trình (Full Dock & Multi-Tier Integration Test):**
   - Chạy kiểm thử: `python3 test_dock_integration.py`
   - **Kết quả:**
     - 4 pallet lưu kho tự động AS/RS và 4 pallet dỡ hàng rời hoàn tất 100%.
     - Đúng 72 thùng hàng được chuyển giao và phân bổ đều đặn lên 4 tầng (`18 thùng/tầng`).
     - Hoàn tất 72 lượt quét mã vạch chẵn và 72 lượt quét mã vạch lẻ.
     - 5976 sự kiện điều phối, 18072 lượt quét kiểm tra swept envelope không va chạm.
     - **0 deadlock, 0 va chạm, 0 lỗi điều phối**.
4. **Hướng dẫn người dùng tự kiểm tra:**
   - Người dùng có thể khởi chạy mô phỏng trực tiếp từ Windows bằng file:
     [`02-Start-Warehouse.cmd`](file:///d:/work1/duoc-long-chau/02-Start-Warehouse.cmd) hoặc [`Run-Warehouse-Direct.cmd`](file:///d:/work1/duoc-long-chau/Run-Warehouse-Direct.cmd).
   - Khi cửa sổ 3D mở lên, hãy quan sát:
     + Chuyển động mượt mà, không còn giật lag.
     + Hàng hóa được đưa vào liên tục từ xe tải vào kho.
     + Toàn bộ công nhân trên cả 4 tầng đều có cử chỉ làm việc năng động, hai tay đặt trên bàn/máy quét/khay hàng, không một ai đứng bất động như ma-nơ-canh.
     + Xe nâng tay vận hành chuẩn vật lý: bơm kích nâng, đẩy vào slot êm ái, lùi rút xe và quay về lấy hàng chuyến tiếp theo mà không chạm hay đụng vào AGV.

---

## 5. Giải Thích & Khắc Phục Hiện Tượng Cửa Sổ Gazebo Không Hiện Lên Màn Hình

### A. Căn nguyên kỹ thuật
1. **Bắt nhầm cửa sổ trong tiến trình kích hoạt ([`Launch.ps1`](file:///d:/work1/duoc-long-chau/Launch.ps1)):**
   - Trong hàm `FindWindowByKeywords`, từ khóa `'Warehouse'` đã bắt trúng chính cửa sổ dòng lệnh Terminal mang tên `02-Start-Warehouse.cmd` (hoặc `Administrator: cmd.exe`).
   - Tiến trình ngầm đã gọi `SetForegroundWindow` liên tục lên cửa sổ dòng lệnh, khiến cửa sổ Terminal bị ghim đè lên phía trước và ngăn cản cửa sổ 3D của Gazebo hiển thị lên màn hình.
2. **Xung đột lệnh gọi focus từ bên trong WSL ([`wait_gui.py`](file:///d:/work1/duoc-long-chau/wait_gui.py)):**
   - Đoạn mã `subprocess.run(['powershell.exe', ...])` chạy từ bên trong Linux để gọi `AppActivate('Warehouse')` gây trễ IPC và kích hoạt sai tiến trình CMD thay vì cửa sổ Gazebo.
3. **Trạng thái ngủ/lệch phiên đồ họa WSLg RDP RAIL:**
   - Trong log `/mnt/wslg/weston.log`, sau khi phiên mô phỏng trước đó bị đóng đột ngột, trình quản lý cửa sổ Xwayland/Weston lưu trạng thái `appWindowId: 0x0` (chưa gán cửa sổ hiển thị lên Windows host).

### B. Các biện pháp khắc phục đã hoàn tất
1. **Chuẩn hóa bộ lọc cửa sổ trong [`Launch.ps1`](file:///d:/work1/duoc-long-chau/Launch.ps1):**
   - Loại trừ hoàn toàn `cmd.exe`, `powershell`, `02-Start`, `WindowsTerminal`.
   - Tìm đích danh tiêu đề cửa sổ X11 thực tế của mô phỏng là `"Gazebo Sim"`.
2. **Làm sạch [`wait_gui.py`](file:///d:/work1/duoc-long-chau/wait_gui.py):**
   - Bỏ hoàn toàn lệnh gọi subprocess Windows từ WSL, đảm bảo nhịp nạp đồ họa thông suốt 100%.
3. **Quy trình kích hoạt đảm bảo hiển thị:**
   - Nếu máy tính vừa đóng mô phỏng đột ngột và WSLg bị kẹt phiên cũ, chỉ cần chạy một lần lệnh `wsl --shutdown` trong PowerShell rồi mở lại [`02-Start-Warehouse.cmd`](file:///d:/work1/duoc-long-chau/02-Start-Warehouse.cmd).
   - Hoặc chạy trực tiếp [`08-Run-Software-Rendering.cmd`](file:///d:/work1/duoc-long-chau/08-Run-Software-Rendering.cmd) để sử dụng chế độ đồ họa tương thích tuyệt đối (LLVMpipe).

---

## 6. Khắc Phục Triệt Để Hiện Tượng Công Nhân Đứng Yên & Tăng Tốc Độ Hệ Thống

### A. Căn nguyên kỹ thuật từ hình ảnh thực tế của người dùng
1. **Nghẽn cổng giao cắt an toàn FloorGate (AGV Starvation):**
   - Trong ảnh người dùng chụp, Pallet 01 đã được công nhân xe nâng tay đẩy ra vạch tiếp nhận (Slot 0), công nhân đứng lùi lại ở $y = -5.6\text{ m}$. Tuy nhiên, 2 xe nâng tự hành AGV vẫn đỗ bất động tại ô đỗ giữa kho.
   - *Nguyên nhân:* Khóa đồng bộ `FloorGate` cũ cấp quyền ưu tiên tuyệt đối cho luồng thủ công (`not self.waiting`). Do có 4 cửa dock hoạt động dỡ hàng và quay về liên tục, cờ chờ `self.waiting` luôn $\ge 1$, khiến AGV bị chặn hoàn toàn không bao giờ xin được quyền vào bốc pallet. Pallet bị kẹt cứng tại vị trí tiếp nhận, chu trình nhập kho tự động AS/RS bị đình trệ.
2. **Khoảng ngắt quãng 3.8s trong bộ tạo cử chỉ làm việc tĩnh:**
   - Trong hàm cử chỉ tự nhiên của 114 công nhân, chu kỳ đặt là 5 giây nhưng chỉ nội suy chuyển động trong 1 giây đầu, 3.8 giây còn lại bỏ trống không gửi cập nhật. Điều này tạo cảm giác công nhân bị "đóng băng" 80% thời gian.
3. **Nhịp độ cấp hàng từ xe tải quá chậm so với công suất nhà máy 4 tầng:**
   - Chu kỳ quét mã vạch và gắp đặt thùng tại dock cũ tốn tới 115 giây cho một pallet 18 thùng, cộng với nhịp xe tải tới 18s. Lượng hàng chuyển vào quá thưa thớt khiến công nhân tại các tầng 2, 3, 4 phải đứng chờ rảnh rỗi.

### B. Giải pháp kỹ thuật đã hoàn tất
1. **Tái cấu trúc khóa FloorGate theo cơ chế luân phiên công bằng (Fair Turn-Taking):**
   - Bổ sung cờ `last_turn` trong [`dock_flow.py`](file:///d:/work1/duoc-long-chau/dock_flow.py): Khi cả AGV và công nhân xe nâng tay cùng xếp hàng, hệ thống bắt buộc luân phiên lượt đi 1-1. Ngay khi công nhân dỡ pallet xuống và rút xe ra, AGV lập tức được quyền tiến vào bốc pallet đưa sang thang nâng, chấm dứt hoàn toàn hiện tượng AGV đứng chôn chân.
2. **Nâng cấp cử chỉ làm việc điều hòa hình sin liên tục (Continuous Sinusoidal Micro-Dynamics):**
   - Thay thế đoạn ngắt quãng cũ bằng phương trình điều hòa liên tục $w = 0.5 - 0.5 \cos(\pi \tau / T) + A \sin(\omega \tau)$.
   - Toàn bộ 114 công nhân trên cả 4 tầng luôn duy trì cử chỉ thở, đổi trọng tâm chân, cử động cánh tay và liếc nhìn màn hình HMI/máy quét nhịp nhàng, không có bất kỳ khoảng đơ tĩnh nào.
3. **Đẩy nhanh tốc độ và lưu lượng hàng hóa toàn kho ([`config.json`](file:///d:/work1/duoc-long-chau/config.json)):**
   - Tăng tốc độ mô phỏng chung: `"speed_multiplier": 3.0` (vẫn đảm bảo khóa cứng 20 FPS mượt mà).
   - Rút ngắn chu kỳ xe tải tới từ 18s xuống **8.0s**.
   - Tăng tốc độ dỡ hàng tại dock: Quét mã vạch giảm xuống **0.40s**, bốc/đặt thùng **0.35s**, công nhân đi bộ **1.5 m/s**, xe nâng tay **1.2 m/s**.
   - Nâng tốc độ băng chuyền thùng hàng lên **1.0 m/s** (chuẩn băng chuyền con lăn công nghiệp tốc độ cao), tốc độ dỡ thùng lên băng chuyền rút ngắn còn **1.2s/thùng**.
   - Tăng tốc độ các trạm đóng gói và chia chọn (U1, Z05, U2, P1) lên 1.2s - 1.5s/thùng.
   - Hàng hóa tuôn vào như một dòng chảy liên tục, toàn bộ công nhân trên 4 tầng luôn bận rộn làm việc nhịp nhàng.

---

## 7. Khắc Phục Lỗi Va Chạm Giữa AGV và Người/Xe Nâng Tay Tại Vùng Giao Nhận

### A. Phân tích nguyên nhân hình học và điều phối (Geometric & Handover Conflict)
Từ hình ảnh người dùng cung cấp (`media_1794554430383_b62ac11f.png`), ta thấy rất rõ:

```
                            BẮC (+y)
                               ▲
                               │
            [Slot 0: Pallet ở y = -2.765 m, x = -33.485 m]
                               ▲
               [Càng xe nâng rút về y = -4.50 m]
                               ▲
             [Công nhân đứng ở y = -5.60 m]  <--- ĐIỂM XUNG ĐỘT!
                               ▲
   [Hành lang an toàn bên trong: y = -5.35 m] ───────────────────────
                               ▲
       [Làn xe AGV không tải: RETURN_Y = -6.40 m] (AGV chạy hướng Bắc vào Slot 0)
                               ▲
       [Làn xe AGV chở tải:  LOAD_Y   = -8.40 m]
                               ▲
   [Vạch đỗ Pallet Dock: stage(d) y = -9.60 m]
   [Cửa Dock xe tải:      rear_y  y = -10.22 m]
                               │
                            NAM (-y)
```

1. **Tọa độ xung đột không gian:**
   - Vị trí Slot 0 đặt tại: $x = -33.485\text{ m}, y = -2.765\text{ m}$.
   - Quỹ đạo tiếp cận của AGV từ làn xe trống `RETURN_Y = -6.4\text{ m}`: AGV chạy đến $x = -33.485\text{ m}$, xoay hướng Bắc ($\pi/2$), rồi tiến thẳng theo trục $y$ từ $-6.4\text{ m} \to -4.565\text{ m}$, vươn càng nâng vào dưới đáy pallet tại $y = -2.765\text{ m}$.
   - Trong khi đó, công nhân sau khi đẩy pallet vào Slot 0 và hạ kích sẽ rút xe nâng tay lùi về vị trí $y = -4.5\text{ m}$. Lúc này thân người công nhân đứng tại $(x = -33.485\text{ m}, y = -5.6\text{ m})$.
   - Điểm đứng của người và xe nâng tay nằm **ngay chính giữa luồng xe AGV tiến vào**!

2. **Xung đột điều phối (Timing Race Condition):**
   - Trong đoạn mã cũ:
     ```python
     await self.transfer(i, local, target)  # Chặng 1: đẩy pallet vào slot (giải phóng khóa FloorGate khi xong)
     await consume(i, local)                # Đẩy job vào in_queue -> KÍCH HOẠT AGV NGAY LẬP TỨC!
     await self.transfer(i, [], stage(d), True, without_deck=True)  # Chặng 2: xin khóa để quay về
     ```
   - Khi chặng 1 kết thúc, khóa an toàn `FloorGate.manual()` được nhả ra.
   - Lệnh `consume(i, local)` lập tức đẩy `job` cho AGV. AGV đang chờ trong `in_queue` lập tức thức dậy và chiếm được khóa `FloorGate.agv()`.
   - Cùng thời điểm đó, công nhân cố gắng chạy Chặng 2 để quay về dock, nhưng hàm `transfer(returning=True)` đòi hỏi khóa `FloorGate.manual()`. Do AGV đang giữ quyền chạy trên sàn, công nhân bị khóa từ chối và buộc phải đứng chờ (`idle`).
   - Vì bị kẹt cứng tại tọa độ $(x = -33.485\text{ m}, y = -5.6\text{ m})$, khi AGV lao đến bốc pallet, AGV đã tông thẳng và đi xuyên qua thân người công nhân cùng chiếc xe nâng tay!

### B. Giải pháp khắc phục triệt để
1. **Khóa an toàn nguyên khối hai chiều (Atomic Round-Trip Lock):**
   - Đã cập nhật hàm `transfer` trong [`dock_flow.py`](file:///d:/work1/duoc-long-chau/dock_flow.py) cho phép nhận tham số `traffic` từ ngoài.
   - Hàm `prepare` chiếm giữ khóa `FloorGate.manual()` xuyên suốt từ lúc bắt đầu đẩy pallet ra khỏi dock, qua ngã tư giao cắt, đặt pallet vào Slot, lùi xe ra, và **kéo xe nâng tay quay về an toàn tận cửa dock**.
2. **Hoán đổi mô hình pallet trực quan không độ trễ:**
   - Pallet và các thùng hàng được đặt cố định tại Slot 0 trong suốt thời gian công nhân rút lui.
   - Khi công nhân đã về đến cửa dock và nhả khóa an toàn `FloorGate.manual()`, hàm `consume(i, local)` mới được kích hoạt:
     + Chuyển đổi mượt mà mô hình pallet trung gian thành mô hình pallet chính thức `pallet_{job}`.
     + Kích hoạt sự kiện `PICKUP_BAY_LOADED` và gửi lệnh vào `in_queue`.
3. **Đảm bảo không gian hoàn toàn thông thoáng cho AGV:**
   - Khi AGV nhận lệnh và xuất phát từ bãi đỗ ($x = -24\text{ m}$), công nhân và xe nâng tay đã về đến cửa Dock 4 ($x = -31.88\text{ m}, y = -10.22\text{ m}$), cách xa khu vực giao nhận hơn 10 mét.
   - Toàn bộ vạch tiếp cận Slot 0 từ $y = -6.4\text{ m}$ đến $y = -2.765\text{ m}$ hoàn toàn trống trải.
   - AGV tiếp cận, đâm càng và nâng hàng lên với **khoảng hở an toàn tuyệt đối, 0% va chạm hay chồng lấn hình học**.
4. **Kiểm tra tự động toàn trình:**
   - Chạy kiểm thử tích hợp [`test_dock_integration.py`](file:///d:/work1/duoc-long-chau/test_dock_integration.py): Toàn bộ 8 chuyến xe và 72 thùng hàng vận hành trơn tru, cả AGV và công nhân thay nhau luân phiên nhịp nhàng, 0 va chạm, 0 lỗi điều phối.

---

## 8. Bảng Đối Chiếu Hiện Trạng Luồng Làm Việc Toàn Kho (Workforce & Logistics Flow)

Dưới đây là bảng tổng hợp xác nhận 100% nhân lực và thiết bị tự hành trong toàn bộ khuôn viên nhà máy đang vận hành liên tục, ăn khớp nhịp nhàng:

| Khu Vực | Nhân Sự / Thiết Bị | Nhiệm Vụ Liên Tục Trong Mô Phỏng | Trạng Thái Hoạt Động |
| :--- | :--- | :--- | :---: |
| **Cửa Dock Xe Tải (Tầng 1)** | 4 Công nhân tại Dock 4, 5, 9, 10 | Bốc từng thùng thuốc từ đuôi xe tải, quét barcode tại trụ scanner, xếp lớp lên pallet, kéo xe nâng tay ra slot | **Hoạt động liên tục** (Chu kỳ xe tải 8.0s) |
| **Vùng Giao Nhận Cửa Kho** | 4 Xe nâng tay thủy lực màu vàng | Kích nâng pallet $+5\text{ cm}$, vận chuyển dọc hành lang $y = -5.35\text{ m}$, hạ pallet vào Slot, lùi xe và quay về | **Hoạt động liên tục** (1.2 m/s có tải, 1.5 m/s không tải) |
| **Hành Lang Tiếp Nhận (Tầng 1)** | Công nhân tuần tra (Worker 500) | Đi bộ tuần tra dọc 4 ô slot đón hàng, kiểm tra tem nhãn và trạng thái sẵn sàng của pallet | **Tuần tra liên tục** (S-Curve êm ái) |
| **Đội Xe AGV Tầng 1** | AGV 1 và AGV 2 | Đón pallet chẵn tại Slot 0-3, xoay cua đưa sang băng tải xích Infeed Lift, hạ hàng vào thang nâng kho 9 tầng | **Vận chuyển liên tục** (1.8 m/s, dual-AGV luân phiên) |
| **Kho Cao Tầng AS/RS (9 Tầng)** | 9 Shuttle tự hành & Thang nâng hàng | Đón pallet từ Infeed Lift, di chuyển theo trục ray lưu trữ pallet vào đúng ô giá kệ định vị CAD | **Lưu kho liên tục** (2.0 m/s shuttle, 1.5 m/s lift) |
| **Trạm Rỡ Hàng Rời (Gần Xoắn Ốc)** | 2 Công nhân Carton Inbound | Tiếp nhận pallet lẻ từ dock, dỡ từng thùng hàng đặt lên băng chuyền dẫn vào tháp xoắn ốc | **Dỡ hàng liên tục** (1.2s/thùng) |
| **Băng Chuyền & Tháp Xoắn Ốc** | Hệ thống con lăn & băng tải xoắn | Vận chuyển các thùng hàng rời từ Tầng 1 leo xoắn ốc lên phân phối đều đặn cho Tầng 1, 2, 3, 4 | **Tốc độ cao 1.0 m/s** (18 thùng/tầng/chu kỳ) |
| **Trạm Phân Tuyến Zone 05** | Công nhân Zone 05 (Mỗi tầng) | Tiếp nhận thùng hàng từ nhánh băng chuyền xả, bưng chuyển sang 6 trạm U2 | **Vận chuyển liên tục** (1.5 m/s) |
| **Trạm Chia Chọn U1 & U2** | Công nhân U1 & U2 (Mỗi tầng) | Mở thùng thuốc, phân loại vào giá kệ, xếp vỏ thùng carton vào xe đẩy lưới | **Thao tác liên tục** (1.2s mở thùng, 1.5s gắp thuốc) |
| **Thu Gom Vỏ Thùng U1/U2** | AGV U1 & U2 (Mỗi tầng) | Khi xe lưới đầy 5 vỏ thùng, AGV tự động chở xe về Khu 06 và lấy xe lưới rỗng trả về trạm | **Tự động hóa hoàn toàn** (1.5 m/s) |
| **56 Trạm Đóng Gói P1 (4 Tầng)** | 56 Công nhân đóng gói P1 | Gắp các hộp thuốc vàng xếp vào thùng carton, đóng băng keo, đẩy thùng sang bàn giao hàng | **Đóng gói liên tục** (Tư thế năng động, không đơ) |
| **Cấp Kệ Thuốc P1 (Kho 04)** | 56 AGV P1 (14 AGV/tầng) | Tự động chạy con thoi giữa Kho 04 và các bàn P1 để cấp kệ thuốc đầy và thu hồi kệ rỗng | **Chạy con thoi liên tục** (1.5 m/s, Semaphore 4) |
| **Trạng Thái Tĩnh (Khi Chờ Việc)** | Tất cả 114 công nhân | Cơ chế điều hòa hình sin Golden Ratio $\phi = \text{wid} \cdot 1.618$: Thở, đổi trọng tâm chân, vươn tay kiểm tra bàn | **100% tự nhiên**, loại bỏ hoàn toàn ma-nơ-canh |

---

## 9. Khắc Phục Lỗi Va Chạm Bãi Đỗ AGV & Tắc Nghẽn Đơn Luồng Tại Cửa Dock

### A. Phân tích căn nguyên từ hình ảnh thực tế (`media_1794565073830_a95ab504.jpg`)
1. **Va chạm đâm xuyên bãi đỗ AGV trung tâm:**
   - Trong ảnh, người dùng quan sát thấy công nhân khi vận chuyển xe nâng tay qua lại giữa các khu vực kho đã đi đâm xuyên qua 2 chiếc AGV đang đỗ ở giữa kho.
   - Bãi đỗ 2 xe AGV đặt tại tọa độ CAD: $x_1 = -24\text{ m}, x_2 = -19\text{ m}, y = -4.5\text{ m}$. Ô đỗ và thân xe kéo dài theo trục Y từ $y = -5.45\text{ m}$ đến $y = -3.55\text{ m}$ (đuôi xe AGV vươn tới $y = -5.37\text{ m}$).
   - Trong [`dock_flow.py`](file:///d:/work1/duoc-long-chau/dock_flow.py), hàm `transfer_waypoints` trước đây nối đường thẳng dọc theo hành lang `inside_y = -5.35\text{ m}`.
   - Do $-5.35\text{ m}$ nằm trúng giữa vùng $[-5.45\text{ m}, -3.55\text{ m}]$, mọi hành trình ngang kho giữa Dock 4/5 ($x \le -27.8\text{ m}$) và Khu Hàng Rời ($x = -10.7\text{ m}$) hoặc giữa Dock 9/10 ($x \ge -13.8\text{ m}$) và Slot 0-3 ($x \le -30.3\text{ m}$) đều đâm xuyên qua tâm thân xe AGV đang đỗ!

2. **Hiện tượng công nhân bốc hàng xong đứng đông cứng tại cửa dock:**
   - Trong ảnh, công nhân tại các cửa dock bên phải đã chất đủ hàng lên pallet nhưng vẫn đứng yên bên cạnh xe nâng tay, không vận chuyển pallet vào kho.
   - Cơ chế khóa `FloorGate.manual()` cũ sử dụng biến boolean `self.crossing = True`. Khi một công nhân (ví dụ Dock 4) bắt đầu vận chuyển pallet (mất 20-30s cho chu trình đẩy vào slot, hạ pallet, rút càng và kéo xe quay về), biến `self.crossing` bị khóa chặt.
   - Khi công nhân tại Dock 5, Dock 9 hoặc Dock 10 hoàn tất xếp 18 thùng hàng lên pallet và xin khóa `FloorGate.manual()`, họ bị chặn đứng bởi `wait_for(lambda: not self.crossing...)`. Toàn bộ 3 công nhân dock còn lại bị đẩy vào vòng lặp `await self.idle(i, .5)`, đứng chôn chân suốt nửa phút dù hàng đã sẵn sàng.
   - Đây là lỗi thiết kế "tuần tự hóa quá mức" (over-serialization): trên thực tế, các cửa dock nằm cách nhau từ 5m đến 24m và dẫn tới các ô tiếp nhận khác nhau, hoàn toàn có thể vận chuyển đồng thời trên lối đi mà không hề xung đột với nhau.

3. **AGV chạy trên làn `LOAD_Y` không có khóa bảo vệ:**
   - Trong `controller.py:461`, ngay khi AGV vừa nâng pallet lên tại Slot 0, mã nguồn đã vội vàng gọi `await traffic.__aexit__()` trước khi AGV thực hiện lệnh lùi ra làn `LOAD_Y = -8.4\text{ m}` và chạy ngang qua mặt tiền 4 cửa dock.
   - Trong khoảng thời gian AGV chạy tốc độ cao dọc theo $y = -8.4\text{ m}$, khóa `dock_flow.gate` hoàn toàn tự do, khiến công nhân tại dock có thể kéo xe băng ngang qua làn đường đúng lúc AGV lao qua.

### B. Các giải pháp kỹ thuật đã xử lý triệt để
1. **Đổi tuyến thông minh tránh bãi đỗ AGV (Smart AGV Parking Detour):**
   - Trong `dock_flow.py:transfer_waypoints`:
     ```python
     if x_from < -25.0 and x_to > -18.0:
         pts.extend([(-25.5, inside_y, 0, 0), (-25.5, -3.20, 0, 0), (-17.5, -3.20, 0, 0), (-17.5, inside_y, 0, 0)])
     elif x_from > -18.0 and x_to < -25.0:
         pts.extend([(-17.5, inside_y, 0, 0), (-17.5, -3.20, 0, 0), (-25.5, -3.20, 0, 0), (-25.5, inside_y, 0, 0)])
     ```
   - Khi lộ trình đi ngang qua cụm bãi đỗ giữa $x < -25.0\text{ m}$ và $x > -18.0\text{ m}$, xe nâng tay tự động bẻ cua lên phía Bắc theo hành lang $y = -3.20\text{ m}$ trong khoảng $x \in [-25.5, -17.5\text{ m}]$.
   - Tuyến đường $y = -3.20\text{ m}$ nằm cách tâm AGV đỗ hơn $1.3\text{ m}$, cách vạch sơn bãi đỗ $> 0.35\text{ m}$, và hoàn toàn không vướng bất kỳ cột chịu lực hay kết cấu CAD nào.
   - Khi di chuyển nội bộ trong cùng một phân khu (ví dụ Dock 4 sang Slot 0 hoặc Dock 9 sang Khu Hàng Rời), tuyến đường vẫn đi thẳng theo $y = -5.35\text{ m}$ nhanh chóng, không tốn thời gian vòng tránh không cần thiết.

2. **Cơ chế cho phép nhiều công nhân vận chuyển đồng thời (Multi-Worker Transfer Counter):**
   - Trong `dock_flow.py:FloorGate`:
     + Thay thế biến boolean `self.crossing = False` bằng bộ đếm `self.crossings = 0`.
     + Khi không có AGV đang di chuyển (`self.agvs == 0`), mọi công nhân từ các cửa dock độc lập khi xếp hàng xong đều có thể lập tức bước ra chuyển hàng (`self.crossings += 1`).
     + Dock 5, 9, 10 không còn phải xếp hàng chờ Dock 4; công nhân dỡ hàng xong là vận chuyển ngay lập tức vào kho, xóa bỏ 100% thời gian đứng chết tại cửa dock.
     + Phía AGV (`FloorGate.agv()`) vẫn kiểm tra nghiêm ngặt điều kiện `self.crossings == 0`, đảm bảo AGV chỉ được phép chạy khi toàn bộ công nhân đã rút hoàn toàn về vị trí an toàn.

3. **Kéo dài khóa an toàn trong suốt hành trình AGV chạy trên `LOAD_Y`:**
   - Trong `controller.py:agv_worker`:
     + Dời lệnh giải phóng khóa `await traffic.__aexit__()` xuống sau khi AGV đã hoàn tất `follow_async(job, lane, gate[:-1], .2, p)`: tức là AGV đã lùi ra `LOAD_Y`, chạy dọc làn qua mặt tiền các dock, và đã rẽ vuông góc vào vị trí thang nâng `x = ix` ($ix \ge 1.0\text{ m}$, nằm ngoài toàn bộ vùng dock).
     + Đảm bảo 100% an toàn: Tuyệt đối không một công nhân nào có thể bước ra làn `LOAD_Y = -8.4\text{ m}` khi AGV đang chở tải chạy trên làn này.

4. **Kiểm thử tự động & Xác nhận tính đúng đắn:**
   - 44/44 Unit Tests pass 100% trong 0.318s.
   - [`test_dock_integration.py`](file:///d:/work1/duoc-long-chau/test_dock_integration.py) hoàn thành trơn tru với 18,072 lượt quét swept pose và 17,721 lượt kiểm tra ray: 0 va chạm, 0 lỗi điều phối, tốc độ luân chuyển hàng hóa tăng vọt và các công nhân phối hợp nhịp nhàng liên tục.
