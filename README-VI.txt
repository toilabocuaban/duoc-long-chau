KHO 4-WAY SHUTTLE - BAN MO PHONG GAZEBO
Ban dong goi CAD: 07/11/2026

1. GOI NAY LA GI?
Day la du an mo phong Gazebo Harmonic, gom ma nguon, mo hinh 3D, cau hinh,
trinh khoi dong va huong dan cai moi truong. Khong phai file EXE doc lap.
May nhan can Internet khi cai dat lan dau. Goi khong kem toan bo Ubuntu/Gazebo.

2. YEU CAU MAY NHAN
- Windows 11 hoac Windows 10 build 19044 tro len, WSL 2 va WSLg.
- Ubuntu 24.04 + Gazebo Harmonic. Setup tao distro rieng ten Gazebo-Ubuntu.
- Khuyen nghi RAM 16GB tro len, driver GPU ho tro WSLg.
- Du dung luong cho Ubuntu va cac thu vien Gazebo (nen du it nhat 15GB).
- Lan cai WSL dau tien co the can quyen quan tri va khoi dong lai Windows.

3. CAI DAT LAN DAU TREN MAY WINDOWS
A. Giai nen toan bo ZIP vao thu muc co quyen ghi. Khong chay truc tiep trong ZIP.
B. Neu chua co WSL 2, mo PowerShell voi quyen Administrator va chay:
     wsl --install --no-distribution
   Khoi dong lai Windows neu duoc yeu cau. Cap nhat WSL bang:
     wsl --update
C. Mo 01-Setup-Windows.cmd. Script se tao Ubuntu 24.04 va cai Gazebo Harmonic.
   Neu WSL cu khong nhan --name/--location: cap nhat WSL, chay lai setup.
D. Sau khi setup bao Ready, mo 02-Start-Warehouse.cmd.

4. CAC LENH SU DUNG
- 02-Start-Warehouse.cmd: chay 18 pallet nhap va 18 yeu cau xuat song song.
- 03-Inspect-Equipment.cmd: mo canh rieng xem chi tiet shuttle va thang nang.
- 04-View-Shuttle.cmd / 05-View-Lift.cmd: doi goc nhin trong canh chi tiet.
- 06-View-Warehouse.cmd / 07-View-AGV.cmd: doi goc nhin trong canh kho.
- 08-Run-Software-Rendering.cmd: thu dung CPU neu GPU bi loi (chay cham hon).
Can mo dung canh truoc khi dung lenh doi goc nhin. Chi mo mot phien cho moi canh.
Lan dau co the mat mot luc de tao mo hinh va nap hang nghin pallet minh hoa.
Dong cua so Gazebo va cua so lenh khi ket thuc. Khoi dong lai de chay tu dau.

5. CHAY TREN UBUNTU 24.04 LINUX
Mo terminal tai thu muc du an:
  sudo bash setup-ubuntu.sh
  bash run.sh
Can phien desktop co do hoa. Voi Linux native, graphics.sh mac dinh dung
software rendering khi khong co /dev/dxg. Co the sua graphics.sh de dung driver
OpenGL native cua may. Cau hinh da kiem tra thuc te la Windows + WSLg.

6. SUA DU AN
- config.json: cao do, kich thuoc pallet, toc do minh hoa, so nhiem vu.
- build_world.py, cad_scene.py: sinh khung kho, ray va hang minh hoa.
- equipment_visuals.py: chi tiet shuttle va thang nang.
- controller.py: trinh tu nhap-cat-xuat va ban giao hang.
- agv_motion.py / rail_geometry.py: tuyen AGV, vung bao va cao do ray.
Mo hinh se duoc sinh lai khi khoi dong, vi vay khong sua truc tiep file SDF/STL.
Khong can cai ROS de chay ban nay. Ten distro mac dinh nam trong Launch.ps1.

7. PHAM VI VA GIOI HAN
- Kho 9 tang luu tru; khu xuat 4 tang tai 0/5/10/15m.
- 2 AGV forklift, 2 thang nhap, 2 thang xuat, 10 shuttle.
- Chuyen dong theo kich ban va kiem tra vung bao; chua phai mo phong dong luc
  hoc tiep xuc, tu tim duong, kiem chung nang suat hay thiet ke co khi che tao.
- 9 pallet dong san de xuat; cac pallet tinh khac la hang minh hoa.
- Puly/xich/cam bien la chi tiet the hien; chua co PLC, WMS hay safety logic.
- Mau sac: shuttle xanh-den, pallet nhua xanh, hang vang nhat.

8. KIEM TRA GOI
Trong PowerShell tai thu muc du an (sau khi setup):
  powershell -NoProfile -ExecutionPolicy Bypass -File .\Launch.ps1 -Mode Test
Lenh se chay test va 18 chu trinh headless trong partition rieng.
MANIFEST.json chua SHA-256 cua cac tep goc de doi chieu truoc lan chay dau.
Khi chay, SDF/STL va nhat ky duoc tao lai nen checksum cac tep nay se thay doi.

9. TAI LIEU CHINH THUC
https://gazebosim.org/docs/harmonic/install_ubuntu/
https://learn.microsoft.com/windows/wsl/tutorials/gui-apps

Video tham khao, PDF layout, tep tam va nhat ky may goc khong nam trong goi.


CAD LA NGUON MAT BANG CHINH
- design_layout.json: toa do trich tu block 4 Ways va kien truc asnsalda cua DWG.
- 1,152 o X/tang, 250 doan khung xanh da gop, 6 cot nha may.
- O X chi dung luu tru; hang minh hoa chi nam tai o X.
- Khung xanh la vat can, shuttle khong duoc di cat ngang.
- San mat cao la duong chung nguoi va shuttle, theo xac nhan cua chu dau tu.
- 6 duong ngang va 2 truc doc, cac cua/thang/buffer theo toa do CAD.
- Bo dieu khien va mo hinh 3D dung chung mot ban do. Khong dung luoi tu do cu.
- Ban demo 18 nhiem vu chon o dau kenh. Chua dieu phoi lay hang sau kenh hoac
  di chuyen pallet chan duong. Nguoi di bo chua duoc mo phong thanh agent.
- 1,152 x 9 = 10,368 vi tri theo DWG nay; khac con so 9,567 o bang thong so cu.
- Cao do 9 tang va khu 4 tang van theo cau hinh da thong nhat.
- Ban ve 2D doi chieu: previews/layout-cad-check.png.
- Mo 11-View-CAD-Top.cmd khi can so sanh mat bang 3D.
- Dang pallet trong kho: X=1.2m, Y=1.0m. Pallet quay cung AGV voi goc gan
  tuong doi co dinh; ban nang shuttle ha rieng, banh van nam tren ray.
- Vi tri cot la footprint CAD, khong phai khoang trang co the chay qua.
- Nguon DWG khong bi thay doi va khong duoc kem trong goi.

DIEU PHOI DONG THOI (ban moi)
- Hai AGV co worker rieng; hanh lang chung duoc dat cho khi di qua.
- Shuttle tai cac tang co the chay dong thoi. Mot shuttle lam viec tren moi
  tang, shuttle thu 10 du phong; khoa tung tang ngan tranh chap tren ray.
- Nhap va xuat la hai hang doi doc lap. Mac dinh 15 giay mo phong/pallet
  nhap va 15 giay mo phong/yeu cau xuat. Yeu cau ban se xep hang cho.
- Mo 12-Configure-Flow.cmd de doi RIENG hai tan suat, luu roi khoi dong lai.
  Hoac sua inbound_interval_s / outbound_interval_s trong config.json.
- Ban demo phat sinh 18 pallet nhap va 18 yeu cau xuat; co 9 pallet dong
  san trong kho de xuat ngay, sau do xuat cac pallet da nhap theo thu tu.
  Sau demo con 9 pallet trong kho.
- Nhip yeu cau khong phai nang suat thuc te. Chong lan khong bo qua buffer
  mot pallet, thang chuyen nhap/xuat, khoang ho cang va khung rack/cot.

CHUYEN DONG EM VA DIEU PHOI DUONG (ban hieu chinh)
- Bo khoa toan tuyen AGV. Dat cho rieng tung doan theo vung quet ca than,
  cang va hang; tuyen lay hang y=-12, mang hang y=-9, ve bai y=-7.
- Profile S-curve 7 doan (jerk+, gia toc deu, jerk-, chay deu, jerk-, giam toc
  deu, jerk+). Quang duong dai chay deu dung toc do cau hinh (agv_speed,
  lift_speed, shuttle_speed); quang ngan tu bo doan chay deu/gia toc deu de
  khong vuot gioi han. Van toc va gia toc ve 0 o hai dau.
- Shuttle: dung shuttle_positioning_s de dinh vi truoc moi lan nang/ha mam,
  nang/ha mam trong shuttle_lift_s, doi bo banh X/Y tai moi lan doi huong
  trong shuttle_turn_s.
- *_availability / *_utilization la thong so nang suat, chua dung trong chuyen dong.
  Gioi han gia toc (m/s2): agv_acceleration=0.5; lift_acceleration=0.3;
  shuttle_acceleration=0.6; conveyor_acceleration=0.25.
- Gioi han jerk (m/s3): agv_jerk=1; lift_jerk=0.8; shuttle_jerk=1.2;
  conveyor_jerk=0.5. Quay AGV co gioi han toc do/gia toc/jerk rieng.
- Bang tai conveyor_speed=0.3 m/s.
- speed_multiplier = he so tua nhanh, AP DUNG CHO TAT CA: dong ho mo phong chay
  nhanh k lan thoi gian thuc. Moi thong so giu gia tri that (toc do, gia toc,
  jerk, thoi gian dung, tan suat hang vao / yeu cau xuat); k = 1 la thoi gian thuc.
- Thang den dung cao do, dung han va giu lift_settle_s=1 giay, sau do moi
  chuyen hang.
- Mo 12-Configure-Flow.cmd de chinh tan suat va mot so thong so chuyen dong;
  cac gioi han khac nam trong config.json. Luu va khoi dong lai de ap dung.
- Day la profile chuyen dong va interlock mo phong dong hoc; chua co dao
  dong co khi, cam bien thuc, PLC hay bo dieu khien servo cua nha san xuat.

AGV CHAY CUNG CHIEU (hieu chinh moi)
- Dat cho theo thoi gian-va-vi tri, khong giu toan doan duong cho mot xe.
  Hai AGV duoc chay dong thoi tren hai lan hoac noi duoi cung lan khi du
  khoang cach. Du doan ca luc xe truoc ham dung tai cuoi doan.
- agv_following_gap_m=1.0 la khoang ho giua cac phan xe/hang khi noi duoi.
  Co the sua config.json hoac 12-Configure-Flow.cmd, roi khoi dong lai.
- Diem re/giao cat van cho neu quy dao theo thoi gian co xung dot. Kiem
  tra hinh hoc rieng than, cang, hang; giu nguyen gioi han gia toc/jerk
  va lien dong dung on dinh cua thang truoc khi chuyen pallet.
