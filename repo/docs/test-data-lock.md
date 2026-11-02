# Khóa chỉnh sửa test

`Problem.is_locked` mặc định `True`: xem cấu hình/test được, sửa test phải bấm
**Mở khóa test và hủy các lượt chấm**. Khi mở khóa, ClueOJ chặn lượt mới và AB
các submission QU/P/G của bài cùng mirror dùng chung bộ test. Submission đã
hoàn tất giữ nguyên kết quả; submission AB không tự được chấm lại.

Tác giả sửa trên form hiện tại. Apply hợp lệ lưu dữ liệu, sinh cấu hình rồi mới
khóa lại và mở chấm. Apply lỗi giữ mở khóa để sửa và thử lại. Đóng tab không
tự mở chấm. Không có model bản nháp, chủ phiên hoặc xử lý hai người sửa cùng bài.

Web và bridge kiểm tra trạng thái bài/root lúc nhận và giao bài. Callback của
lượt bị AB không ghi kết quả mới; dispatch token đang có phân biệt các lượt
chấm lại của cùng submission. Slot judge vẫn được giải phóng khi lượt cũ kết thúc.

Judge dùng chung giữ shared `flock` trong lúc đọc dữ liệu và cleanup. Apply dùng
exclusive lock tại `.locks/problem_<root-id>.lock` trên cùng volume, nên chưa
thể thay file khi worker cũ còn đọc, kể cả lệnh abort bị trễ hoặc bridge restart.
Không dùng cooldown cố định để suy ra worker đã dừng. Các file lock là file thường
cho judge đọc được; không xóa hoặc thay file lock trong lúc vận hành.

Triển khai migration `0253_problem_test_data_lock` cùng code web/worker/bridge và
build lại judge từ source `newjudge` đã cập nhật. Bản thay đổi worker/packet riêng
nằm ở [patch judge](patches/judge-test-data-lock.patch); patch này dành cho source
judge đang dùng metadata batch-scoring trong workspace. Chạy `git apply --check`
trên source judge trước khi áp dụng và build lại image. Judge cũ không giữ shared lock,
vì vậy cần cập nhật các judge cùng dùng volume trước khi sử dụng Unlock. Test
data vẫn mount chỉ đọc trong container judge. Phần phân phối/hash cho judge ở
server khác chưa thuộc thay đổi này.

Giữ các ràng buộc cumulative và quyền hiện có. Mở khóa không vượt qua khóa
cumulative. Update Polygon của bài đã có cũng cần mở khóa; trang GET không tự
tạo ProblemData hoặc sửa metadata archive. Những chỉnh sửa trực tiếp qua SSH
nằm ngoài gate của giao diện. Storage service cần tuân theo cùng khóa file để
restore/evict không ghi xen với Apply; phía OJ loại bài đang mở khóa khỏi tác vụ
eviction. Không có thay đổi API hay giao thức storage trong đợt này.

Transaction DB không rollback file đã ghi. Nếu ghi lỗi giữa Apply, bài tiếp tục
mở khóa; cần sửa hoặc upload lại và Apply thành công trước khi phục vụ. Không
có tự khôi phục nguyên bộ cũ.

Kiểm thử chính: `judge.tests.test_test_data_lock`, `judge.tests.test_bridge`,
`judge.tests.test_bridge_django`, batch submission/concurrency và scoring.
Test concurrency dùng MariaDB riêng. Kịch bản E2E chạy judge thật: bài dài + bài
trong queue → Unlock → cả hai AB → thay test → Apply → lượt mới AC, slot được dùng
lại và các submission AB giữ nguyên AB.
