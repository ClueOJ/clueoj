# Điểm tích lũy theo subtask

Tính năng được bật riêng cho từng bài không organization-private, kể cả bài chưa public. Chỉ superuser có thể
bật, tắt hoặc thử rebuild lại trong trang chỉnh sửa bài (`/problem/<code>/edit`)
và trang chỉnh sửa bài trong admin. Panel tại hai nơi này hiển thị trạng thái,
tiến độ và lỗi; có liên kết Refresh để cập nhật. Trang bài chỉ hiển thị bảng điểm
tích lũy của user. Mọi user được tính điểm theo chế độ của bài đó.

Nút bật/tắt dùng form riêng với CSRF và action ẩn, nằm ngoài form lưu nội dung
bài. Lưu thay đổi của bài trước khi bật/tắt. Sau thao tác, quay về đúng trang
chỉnh sửa hoặc admin vừa dùng để tiếp tục theo dõi tiến độ.

Khi bật, điểm đóng góp của bài vào tổng điểm user và đầu vào PP là tổng điểm
cao nhất của từng subtask qua các bài nộp full-test hợp lệ. Ví dụ hai bài nộp
đạt `(30, 0)` và `(0, 70)` đóng góp 100 thay vì 70. Vẫn tính min/sum bên trong
mỗi subtask trước, rồi mới chọn max giữa các bài nộp. Không ghép điểm từng test
của các chương trình khác nhau để vượt qua một subtask min.

Khung điểm tích lũy, từng subtask và ô điểm test trong PP breakdown hiển thị
theo thang điểm gốc của bộ test. Ví dụ test có tổng 200 điểm, đạt 130 thì hiện
`130 / 200`. Nếu thang điểm bài (`Problem.points`) là 40, điểm đóng góp vào
Users/PP vẫn là `130 / 200 × 40 = 26`; cách quy đổi này giữ nguyên.

Giữ nguyên công thức trọng số và bonus PP. Điểm/status của submission và cách
tính AC, số bài giải được, BXH contest vẫn theo luồng hiện có. Đạt điểm tích
lũy tối đa không tự tạo một submission AC.

Điều kiện bật không phụ thuộc `is_public`. Phạm vi tổng điểm/PP công khai vẫn
theo quy tắc hiện có: bài chưa public không đóng góp vào Users/rank; khi công
bố, bài đóng góp theo chế độ tích lũy đã bật. Người có quyền xem bài chưa public
vẫn xem được khung điểm của mình; quyền truy cập bài không thay đổi.

## Điều kiện bật

- Bài không organization-private, cho phép điểm thành phần, điểm bài > 0.
- Test được quản lý tại site hoặc bộ test manual tĩnh hợp lệ, có ít nhất một subtask full-test không rỗng và
  tổng điểm full-test > 0. Subtask 0 điểm được phép nếu tổng điểm vẫn dương.
- Không có bất kỳ test lẻ nào, kể cả test 0 điểm, sample hoặc pretest.
- File archive và `init.yml` hợp lệ. Bài do site quản lý phải khớp cấu hình đã áp dụng.
- Từ chối external/generator, mirror và nguồn đang có mirror.
- Từ chối lịch sử có submission đã khóa, offline chưa công bố hoặc contest
  chưa kết thúc. Không tự bỏ qua hay ép chấm lại lịch sử đó.

### Bộ test quản lý thủ công

Bài manual không cần có các dòng `ProblemTestCase` hay archive trong `ProblemData`.
Web đọc `init.yml` trực tiếp từ thư mục bài trong `DMOJ_PROBLEM_DATA_ROOT`.
`test_cases` và `pretest_test_cases` phải là danh sách tĩnh, mỗi phần tử là một
nhóm `batched` không rỗng, chứa các test có đường dẫn `in`/`out` tường minh
(có thể kế thừa từ cấu hình cha). Input/output có thể là file rời hoặc nằm trong
archive được khai báo. Custom grader, checker và nguồn phụ trợ phải tồn tại như
các file rời mà judge có thể đọc. Không chạy grader hay mã cấu hình trên web.

MIN/SUM lấy từ `ProblemData.legacy_batch_type` và `batch_scoring_overrides`,
giống chấm manual hiện có; khi không có record này, default vẫn là SUM.
Không suy ra layout từ submission cũ. Các dạng tự dò test từ tên file,
khóa động `+`/`++`, generator, nhóm lồng nhau, dependency giữa các nhóm và
symlink chưa được hỗ trợ khi bật cumulative; báo lỗi thay vì đoán cấu trúc.
Vẫn từ chối test lẻ ở cả full và pretest dù chỉ có 0 điểm. Không có pretest
được lưu thành `[]` để nhận đúng lượt full-test fallback của judge cũ.

Khi bật, `subtask_score_config` lưu layout full/pretest và `manual_files`
(`version`, SHA-256 của tên và nội dung toàn bộ file trong thư mục bài).
Không thêm bảng/field DB hoặc đổi giao thức judge. Bridge dùng layout đã chốt
để xác minh batch/count/tổng điểm của kết quả. Manual đang OFF vẫn giữ snapshot
và hành vi chấm cũ, không đọc file để tính cumulative.

Web, bridge và judge phải dùng cùng bộ file đã triển khai; thư mục bài phải
được giữ chỉ đọc khi cumulative đang khóa. Kiểm tra fingerprint trước khi
dispatch, khi nhận kết quả và trước khi công bố rebuild. Sửa input, archive,
checker/grader hoặc thêm/xóa file đều làm kiểm tra thất bại, dù số test và tổng
điểm không đổi. Hash nội dung từng file được tái sử dụng trong mỗi process khi
inode, size, mtime và ctime chưa đổi; fingerprint lưu trong DB chỉ phụ thuộc
nội dung, nên restore đúng cùng bộ file vẫn hợp lệ.

Lượt chấm có file thay đổi được báo IE, không góp điểm. Rebuild gặp lỗi giữ
FAILED và khóa, không công bố ON; retry chỉ dùng lại bộ file đã chốt. Khi đang
ON, các kết quả cũ đã xác minh vẫn được giữ, còn lượt mới có file lệch bị từ
chối. Tắt và đợi OFF vẫn được phép để sửa file, rồi bật lại sẽ chốt fingerprint
mới và chấm lại toàn bộ. Không sửa file ngoài web trong lúc có lượt chấm:
fingerprint phía web không thể xác nhận một máy judge từ xa đang dùng bản gì,
hoặc phát hiện việc đổi rồi hoàn nguyên file hoàn toàn giữa hai lần kiểm tra.

## Bật, tắt và sửa test

`OFF → REBUILDING → ON`; lỗi chuyển thành `FAILED` và giữ khóa.
Tắt từ các trạng thái đang khóa chuyển qua `STOPPING → OFF`.

Mỗi lần bật chốt thế hệ và cấu hình, khóa toàn bộ đường sửa test/cấu hình chấm,
đợi lượt chấm cũ hoàn tất, rồi **chấm lại full tests toàn bộ lịch sử**. CE là
kết quả 0 hợp lệ; IE/AB/kết quả thiếu không được coi là rebuild thành công.
Sau đó tính lại tổng điểm và PP cho mọi user liên quan, rồi công bố ON.
Trong REBUILDING/FAILED/STOPPING tạm chặn nộp và rejudge ngoài job của bài.

Khi tắt, job cũ mất quyền công bố; hệ thống đợi các lượt đang chấm kết thúc,
tính lại profile theo max submission, rồi mới mở khóa sửa test. Muốn đổi test,
checker/grader, min/sum, trọng số, giới hạn hoặc thang điểm: tắt, đợi OFF,
sửa và áp dụng test, sau đó bật lại. Không ghép kết quả qua các thế hệ.
Tên bài, nội dung đề và public/private vẫn sửa được theo quyền hiện có khi đang ON.
Muốn chuyển sang organization-private phải tắt tính năng trước.

Worker xử lý từng submission, lưu tiến độ trong DB. Giao hàng trùng hoặc job
đến muộn phải khớp cả thế hệ và submission đang chờ. Timeout mặc định 3 giờ
không có tiến triển (`SUBTASK_SCORE_REBUILD_TIMEOUT`, giây), không giới hạn
tổng thời gian của một rebuild lớn vẫn đang chạy. SU có thể thử bật lại từ
FAILED hoặc tắt để xử lý lỗi. Nếu STOPPING còn lượt QU/P/G bị kẹt, cần xử lý
judge/queue hoặc hủy lượt đó trước; không mở khóa file trong lúc judge còn dùng.
Retry restore từ storage giữ nguyên revision và chế độ full-test. Retry thuộc
rebuild đã bị thay thế chỉ hủy lượt đang chờ restore của chính revision đó,
không gửi chấm hoặc hủy lượt chấm mới hơn.

## DB và điểm hiển thị

Migration `0247_cumulative_subtask_scoring` thêm vào `Problem`:

| Trường | Nội dung |
|---|---|
| `subtask_score_state` | OFF/REBUILDING/ON/STOPPING/FAILED, mặc định OFF |
| `subtask_score_revision` | Thế hệ tăng mỗi lần bật/tắt |
| `subtask_score_task_id` | ID công việc hiện tại |
| `subtask_score_config` | Snapshot cấu hình đã khóa |
| `subtask_score_progress` | Phase, giới hạn lịch sử, cursor, pending, số lượt và thời gian tiến triển |
| `subtask_score_error` | Lỗi để superuser xử lý |

Tái sử dụng `Submission.batch_scoring` cho snapshot/revision/dispatch token và
`SubmissionBatchResult` cho điểm batch; không tạo bảng tổng user–bài mới.
Điểm bài được đọc từ kết quả đã hoàn tất, không cache tổng dẫn xuất này.
`Profile.points` và `Profile.performance_points` vẫn lưu trong DB, được tính
lại dưới khóa hàng Profile; Users/rank dùng các giá trị đó. Bước refresh cuối
và công bố ON/OFF nằm trong cùng transaction: lỗi sẽ rollback toàn bộ thay
đổi profile/PP. Khi bật tính năng, chỉ job refresh được
đọc chế độ tích lũy chưa công bố; request thông thường dùng max submission
cho đến khi transaction commit. Trang bài, bảng
bài của user và PP breakdown dùng chung nguồn điểm.

Submission đang ẩn, pretests thật hoặc kết quả thiếu/sai thế hệ không tham gia.
Full-test submission của contest đang diễn ra chưa đóng góp vào tổng; job
refresh khi contest kết thúc cập nhật lại profile. Offline publication dùng
luồng refresh profile hiện có. Kết quả mới, rejudge và xóa submission tốt nhất
đều tính lại điểm từ dữ liệu hiện hành.

## Kiểm chứng

Test chính: `judge.utils.tests.test_subtask_scoring`, `judge.utils.tests.test_manual_subtasks` và
`judge.utils.tests.test_subtask_scoring_migration`. Các test đồng thời cần
MariaDB (SQLite không kiểm chứng khóa hàng). Chạy chung với batch scoring,
weighted batches, Polygon, profile/submission, batch-submit và offline.

Đã chạy Celery worker, bridge và Python judge thật với DB test riêng: 30/70 → bật 100 → tắt
70; nguồn hai subtask tách biệt; không tăng số bài AC. Khi ON, xóa submission
70 đưa tổng về 30, nộp mới đưa lên 100, rejudge CE đưa về 30, sửa source và
chấm lại lên 100. Tắt, đổi trọng số thành 40/60, áp dụng test rồi bật lại đã
chấm đúng 40/60 và tổng 100.

Bộ 224 test liên quan pass trên MariaDB 12.2.2, gồm migration và kết nối đồng
thời thật. Bộ hồi quy rộng 359 test có 17 lỗi trong problem/mirror/storage;
chạy mã trước thay đổi trên cùng DB test tái hiện đúng 17 lỗi đó, không có lỗi
mới. Các lỗi cũ gồm fixture vượt độ dài code, test mirror còn kỳ vọng cơ chế
sync đã tắt, host test storage không có trong cấu hình local, cùng một số kỳ
vọng download/date filter của storage.

Thay đổi điều kiện cho phép bài chưa public có test riêng cho bật/rebuild,
hiển thị điểm theo quyền xem bài, chuyển public/private và phạm vi Users/PP.

Deploy cần migrate, compilemessages và restart web/Celery/bridge. Migration
không tự bật bài nào và không tự rejudge toàn site.
