# Mốc điểm tham khảo

Nhập tại Django admin → Exam tags → Mốc điểm tham khảo. Chỉ tài khoản đang
hoạt động có `is_superuser` được chỉnh metadata, xem nguồn nội bộ và dùng
history/recovery của ExamTag. Staff vẫn chỉnh được các field đề thi cũ;
xóa đề có metadata bị chặn để tránh cascade. Revision theo cả hai inline.

Ngữ cảnh điểm chung và riêng đều tùy chọn. Chỉ điền khi cần giải thích
thang điểm hoặc cách tính; mốc như “Giải Nhất” có thể để trống cả hai. Điểm chấp nhận dấu
chấm hoặc phẩy, tối đa bốn chữ số lẻ; checkbox mặc định tắt. Điểm đối chiếu
lấy từ ExamUserProgress, không cộng lại submission trong trang chi tiết.
Nguồn nội bộ không nằm trong serializer public; state cá nhân chỉ tạo khi
render HTML. Thay đổi progress có hiệu lực sau job đồng bộ và refresh.

## Đồng bộ

Snapshot schema version 2. Web fallback và Celery dùng chung file lock
`.build.lock` trên volume snapshot (POSIX flock). Không xóa file lock khi
đang vận hành. Mọi tiến trình ghi snapshot phải dùng helper này và cùng
volume có hỗ trợ flock; không dùng filesystem không bảo đảm advisory lock.

Mỗi sự kiện save/delete đã commit gửi một task, không còn gộp bằng TTL key
vì cơ chế cũ có thể mất cập nhật. Các task ghi tuần tự; một lần lưu nhiều
inline có thể tạo nhiều lượt rebuild. Theo dõi độ dài hàng đợi khi nhập nhiều
mốc. Nếu broker/worker lỗi, sửa dịch vụ rồi rebuild lại snapshot. Bulk update
không phát signal: code gọi bulk phải chủ động queue sau commit.

## Triển khai

1. Dừng chỉnh metadata và đợi job cũ hoàn tất; dừng worker cũ.
2. Đưa code mới lên, chạy `python3 manage.py migrate --noinput`.
3. Build bằng `bash make_style.sh`, chạy collectstatic và pipeline static
   của môi trường. Không sửa CSS sinh ra.
4. Chạy `build_exam_snapshots()` từ Django shell bằng code mới.
5. Khởi động web/worker cùng phiên bản; kiểm tra list/detail và hai API.
6. Nhập cutoff thật có nguồn bằng tài khoản superadmin. Không seed số ví dụ.

Rollback code có thể giữ lại bảng/field mới; đưa web và worker về cùng phiên
bản rồi rebuild snapshot. Không xóa schema để tắt tính năng.

## Kiểm chứng local ngày 20/09/2026

- `manage.py check`: đạt.
- 20 test milestone/progress/list-sort: đạt, DB test riêng, snapshot tạm,
  mock dispatch; không gửi task kiểm thử vào worker thật.
- Migration 0231 đã áp dụng; build hai theme và snapshot 296 đề thành công.
- Browser: trang thư viện hiện có tải thành công; component 15 mốc ở 320px,
  light/dark, mở ghi chú và Escape đạt. Dữ liệu QA được rollback, không seed.
- `makemigrations --check --dry-run` còn báo hai thay đổi đã có trước ở
  StorageEvictionRule.id và StorageUsageSample.id; không gộp vào feature.
- Chưa triển khai production hoặc nhập cutoff thật. Kiểm tra thực tế trên
  thiết bị cảm ứng, tắt JS và toàn bộ quy trình nhập admin ở staging vẫn nên
  thực hiện trước khi phát hành production.

Lệnh test từ thư mục triển khai local:

```sh
./scripts/local exec -T site python3 manage.py test \
  judge.models.tests.test_exam_score_milestones \
  judge.models.tests.test_exam_progress \
  judge.models.tests.test_exams_list_sort --noinput --keepdb
```

Trong cột Tiến độ của bạn, hiện “Mốc cao nhất đạt được: …” theo điểm
mốc lớn nhất trong các mốc đã đạt và bật tự động đối chiếu. Nếu bằng điểm,
chọn mốc đứng trước theo thứ tự quản trị. Nếu có mốc public nhưng không có mốc bật đối chiếu nào đã đạt, kể cả khách,
hiện “Có mốc điểm tham khảo”. Đề không có mốc public không hiện dòng nào.
Cột Đề thi không hiển thị mốc hoặc phần mở nhanh; xem đầy đủ tại trang chi tiết.

Mỗi mốc trong admin chỉ có một ô Giải thích (tùy chọn). Ô Ngữ cảnh điểm riêng
đã bỏ khỏi form; dữ liệu cũ được hiển thị trong giải thích và gộp vào ô này
khi lưu lại. Giữ cột DB cũ để bảo toàn revision. Biểu tượng ⓘ nằm cạnh tên
mốc, không có khung disclosure riêng; hover/focus để xem, click/chạm để giữ
mở, Escape để đóng. Kiểm thử mới nhất: 13 test mốc điểm đạt.

Trang chi tiết: toàn bộ khối Mốc điểm tham khảo là spoiler native details,
mặc định đóng. Bấm tiêu đề để mở/đóng; vẫn hoạt động khi tắt JavaScript.

Tiêu đề spoiler là “Xem mốc điểm tham khảo”. Mở spoiler chỉ hiện lời nhắc
về ảnh hưởng đến phân bổ thời gian/lựa chọn bài; xác nhận “Xem mốc điểm”
mới hiện dữ liệu. Hủy đóng spoiler. Đóng rồi mở lại cần xác nhận lại.
Hai bước dùng native details nên vẫn giữ được khi không có JavaScript.

Danh sách có checkbox “Đối chiếu mốc điểm”, mặc định bật. Tắt thì đề có
mốc chỉ hiện “Có mốc điểm tham khảo”. Lựa chọn đi theo query string
compare_milestones, giữ qua phân trang và lọc đề. Không ảnh hưởng trang chi tiết.

Các nhãn và trợ giúp của tính năng đã có bản dịch tiếng Anh trong
locale/en/LC_MESSAGES/django.po, cùng bản tiếng Việt tường minh để không
fallback sang tiếng Anh. Tên mốc và nội dung quản trị nhập không tự dịch.
Sau cập nhật catalog chạy compilemessages và restart web. Test mới nhất:
19 test mốc điểm/danh sách đạt, gồm kiểm tra đổi locale Anh/Việt.
