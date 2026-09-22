# Thi thử offline từ thư viện đề

Trong Django admin > Exam tags, đặt `duration_minutes` và bật
`virtual_offline_enabled`. Cờ mặc định tắt; người dùng bắt đầu từ trang đề.
Phiên chỉ hỗ trợ bài dùng bộ chấm ClueOJ và ngôn ngữ C++.

Mỗi người dùng có tối đa một lượt thi hoạt động: contest (kể cả virtual/
spectate) hoặc phiên offline. Phải thoát lượt trước để bắt đầu lượt mới.
Các thao tác nhận bài và chuyển lượt khóa cùng dòng Profile trong transaction.
Một OneToOne nullable trên active_user bổ sung bảo vệ ở DB cho phiên offline.

Phiên giữ thời lượng, danh sách bài và thang điểm tại thời điểm bắt đầu.
Mọi lần nộp trong phiên được liên kết với Submission hiện có. Lần cuối được
nhận trước hạn được tính, kể cả CE. Kết thúc sớm chỉ chốt bài, không công bố kết quả. Chỉ khi bấm Xem kết quả,
revealed_at mới được ghi và chủ submission mới xem được kết quả. Dấu AC, điểm hồ sơ
và tiến độ không tính submission chưa reveal. Submission luyện tập riêng vẫn
được tính bình thường. Kết thúc sớm không thể hoàn tác; làm lại
sẽ tạo phiên khác. Điểm lịch sử được tính từ submission đã chốt, vì vậy rejudge
có thể cập nhật điểm lịch sử, nhưng không thay submission được tính.

Trong phiên, middleware chỉ cho truy cập trang phiên, đề, nộp bài và code của
chính phiên, danh sách submission của phiên, cùng các endpoint xác thực cần thiết.
Staff vẫn truy cập Django admin theo quyền hiện có. Các trang thống kê, best
submissions, editorial và API bị khóa. Submission chưa reveal hiển thị --- với chủ bài; người khác xem theo quyền
thông thường. Tiến độ thư viện, dấu AC, điểm hồ sơ, thống kê và websocket
vẫn loại submission chưa reveal để tránh phản hồi gián tiếp cho chủ bài.
Không chấm ở client; đồng hồ giao diện chỉ để hiển thị.

## Vận hành

1. Chạy `python3 manage.py migrate --noinput`.
2. Restart web, Celery worker và judge bridge để cùng dùng schema/code mới.
3. Celery beat chạy `judge.tasks.exam_offline.expire_offline_attempts` mỗi 30 giây.
   Khi người dùng truy cập sau hạn, middleware cũng kết thúc phiên ngay.
   Quyền nộp luôn kiểm tra hạn server, không phụ thuộc beat.
4. Celery worker cập nhật lại tiến độ công khai sau khi reveal phiên.

Các test tự động mô phỏng judge callback; cần kiểm tra compile/run thực tế
với judge và dữ liệu test đầy đủ khi triển khai.

## Kiểm thử

`python3 manage.py test judge.models.tests.test_exam_offline judge.models.tests.test_exam_offline_concurrency judge.models.tests.test_exam_progress judge.models.tests.test_contest judge.models.tests.test_profile.ProfileTestCase --noinput --keepdb`

Race tests dùng các connection độc lập và cần DB hỗ trợ SELECT FOR UPDATE.

## Đề nhiều ngày

Đặt `day_count` trên đề và `day_number` trên từng dòng bài của đề trong admin.
Đề cũ mặc định không chia ngày (day_count=0); ngày của từng bài mặc định 1
và chỉ được dùng khi bật chia ngày. Người dùng chọn ngày khi
bắt đầu virtual; mỗi lượt chỉ chứa bài của ngày đó, có thời hạn và reveal riêng.
Có thể làm bất kỳ ngày nào trước và làm lại từng ngày. Thời lượng của đề áp dụng
cho mỗi ngày. Sửa ngày của bài không làm thay đổi danh sách bài đã chụp của lượt cũ.

Mặc định `day_count=0`: không chia ngày, virtual lấy toàn bộ bài; ẩn bộ chọn,
cột và nhãn ngày. Khi đặt số ngày lớn hơn 0, lưu đề rồi gán ngày từng bài.
Các lượt không chia ngày lưu day_number=0. Cấu hình ngày đã có được giữ nguyên.
