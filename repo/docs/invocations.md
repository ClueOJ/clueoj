# Invocations

Invocations quản lý source theo bài trong trang `test_data`, tạo submission
thông thường của người chạy và đối chiếu kết quả với kỳ vọng overall/subtask.
Chỉ áp dụng với bài root chấm nội bộ và user vừa có `judge.spam_submission`
vừa có quyền sửa bài. User khác giữ nguyên giao diện sửa test; các URL riêng
của tính năng trả 404.

## Cấu hình và triển khai

Trong `local_settings.py`:

```python
DMOJ_INVOCATION_SOURCE_ROOT = '/invocation-sources/'
```

Cũng có thể cung cấp biến môi trường cùng tên. Thư mục phải được mount bền
và dùng chung cho web/worker thực hiện thao tác source. Không expose root
qua nginx/media; tải file qua endpoint có kiểm tra quyền. Source không nằm
trong thư mục test nên không bị thay khi upload ZIP hoặc dọn test local.
Backup root này cùng database. Path dùng problem ID, source UUID và revision
UUID, nên đổi mã bài không đổi vị trí source. DB giữ tên gốc và cấu hình.

1. Merge/deploy theo thứ tự **#25 → #26 → #27**. PR #27 dựa trên nhánh của
   #26 để review; chỉ đổi base về `main` và merge sau khi #26 đã merge.
2. Áp dụng [patch judge Invocations](patches/judge-invocations.patch) trên source
   judge đã có MIN/SUM scoring và patch khóa dữ liệu của #25. Chạy
   `git apply --check` trước khi áp dụng, rồi build lại judge. Patch chỉ bổ sung
   chính sách Invocations và ba kiểm thử chạy bằng
   `python -m unittest dmoj.tests.test_invocations` sau khi áp dụng.
   Judge bỏ dependency/short-circuit giữa các group cho invocation, giữ MIN
   early exit. Submission thường giữ hành vi cũ. Khi khởi động judge, luôn
   dùng `--no-watchdog`; giữ cơ chế refresh qua API private của deployment.
3. Áp dụng `0257_problem_invocations` và `0258_invocation_source_unique_filename`
   sau `0256_conservative_ranking` của #26. Tên source
   phải duy nhất trong mỗi bài; dữ liệu cũ cần hết tên trùng trước khi migrate.
4. Cấu hình root rồi cập nhật/restart web, bridge và Celery worker.
5. Smoke test một bài root gồm SUM/MIN và các source AC/PAC/WA/TLE/CE.

Không chạy invocation trên judge cũ: judge đó có thể vẫn bỏ group phụ thuộc
và không cung cấp báo cáo đầy đủ như yêu cầu.

## Source và chạy bài

Upload chỉ lưu source, chưa tạo submission và không chặn CE. Đuôi C++ chọn
CPP20, `.py` chọn PYPY3, `.pas` chọn PAS nếu runtime được bài cho phép.
Có thể đổi runtime. Runtime khác chỉ được chọn khi có hỗ trợ
comment header; source dạng file-only/output-only không được dùng.

Tên file phải duy nhất trong mỗi bài, kể cả giữa các user. Upload trùng với
file đã có hoặc trùng trong cùng đợt đều báo lỗi kèm tên file; toàn bộ đợt
upload bị hủy, không ghi đè hay tự đổi tên. Các bài khác vẫn có thể dùng cùng
tên file. Upload được khóa theo bài và DB có ràng buộc tên duy nhất.

Bảng cấu hình có một nút **Save all** cho ngôn ngữ và trạng thái của mọi file
đang hiển thị. Bấm ô Overall/group hoặc checkbox trong ô để chọn/bỏ chọn;
Shift+bấm chọn thêm một vùng chữ nhật, Escape bỏ vùng chọn. Chọn status và
bấm **Apply** để đổi các ô đã chọn trên giao diện, rồi **Save all** để lưu toàn
bảng trong một transaction. Nếu có hàng không hợp lệ, toàn bộ thay đổi được
giữ trên giao diện và chưa ghi vào DB. Nút chạy bị tắt khi còn thông số chưa lưu.
Chọn các source bằng checkbox ở cột tên file, rồi dùng **Run selected sources**
hoặc **Delete selected sources**. Xóa chỉ loại các source đã chọn khỏi thư viện;
submission và snapshot của các invocation trước được giữ lại. Không có chức năng
thay file hoặc nút xóa riêng ở từng hàng.

Source dùng UTF-8, không chứa NUL và tối đa 65536 ký tự kể cả header khi chạy.
Mỗi source phải có runtime và kỳ vọng overall trước khi chạy. Kỳ vọng từng
group là tùy chọn. Cấu hình group được kiểm tra lại khi cấu trúc test thay đổi.
Với bài manually managed, web đọc cấu trúc `test_cases` tĩnh trong `init.yml`;
nếu chưa đọc được cấu trúc, vẫn có thể chạy với kỳ vọng overall và xem cấu trúc
thực tế do judge trả về, chưa cấu hình kỳ vọng group.

Mỗi lần bấm chạy tạo invocation và các submission mới trong cùng transaction,
ngoài contest, không thay đổi contest/offline đang mở của user. Mỗi submission
có header comment theo runtime:

```cpp
// Invocations by user alice at 2026-10-31 09:30:00 +07:00
```

Submission giữ code của lượt chạy trong `SubmissionSource`. Item của invocation
giữ snapshot tên, runtime và kỳ vọng. Xóa source không thay bản chụp cũ.
Chủ động chạy lại tạo đợt mới. Retry dispatch gửi lại các submission đã có,
không tạo thêm submission; dispatch token dùng lại để bridge tránh chạy trùng.

Invocation luôn có priority 3, ngang batch rejudge, kể cả restore/rejudge.
Luôn dùng full testset, chạy đủ SUM/test lẻ và cho MIN dừng sớm trong group.
Dùng nguyên điều kiện public/private, điểm, AC, streak và thống kê của
submission thường. Không dùng `batch_rejudge=True` để giả lập priority cho
lượt nộp mới vì flag đó làm thay đổi ngữ nghĩa rejudge/streak.

## Kết quả và kỳ vọng

| Kỳ vọng | Chấp nhận |
| --- | --- |
| AC | Các test được chấp nhận và full điểm |
| PAC | Các test được chấp nhận, thấp hơn full điểm; có thể là 0 |
| AC/TL | AC, PAC hoặc TLE |
| WA | WA |
| TLE | TLE |
| RJ | WA/TLE/MLE/OLE/IR/RTE/CE |
| Skipped | Không kiểm tra kỳ vọng, vẫn chạy và báo kết quả |

PAC được phân loại trong báo cáo từ verdict AC và điểm chưa làm tròn;
không thay enum verdict hoặc điểm thành tích của submission. WA có điểm
không được gọi là PAC. Group và overall dùng thứ tự ưu tiên lỗi của judge:
WA, MLE, TLE, IR, RTE, OLE. CE không có kết quả chạy group: báo CE cùng lỗi
biên dịch, không giả số đo; RJ chấp nhận, các kỳ vọng AC/PAC/WA/TLE không khớp.
IE/AB/SC không thuộc RJ. Test SC sau một lỗi đã xác định trong MIN không
che verdict lỗi của group. Tổng điểm tối đa bằng 0 với mọi test AC được coi AC.

SUM có một dòng cho mỗi test, ghi rõ group. MIN có một dòng group với thời
gian lớn nhất trong các test thực sự đã chạy. Phần tổng hợp ghi kỳ vọng,
thực tế và khớp/không khớp; Skipped không có màu kiểm tra. Báo cáo cập nhật
bằng polling, dừng khi mọi submission kết thúc.

Rejudge trực tiếp submission cũ cập nhật kết quả hiện tại của cột đó và hiển
thị thời điểm rejudge. Nếu cấu trúc test thay đổi, báo CHANGED cho phần test/group
thay vì so nhầm chỉ số. Lịch sử không lưu toàn bộ nội dung test: tạo đợt mới
khi cần so sánh các source trên bộ test hiện tại.

## Kiểm thử

`judge.tests.test_invocations` kiểm tra quyền, nguồn file, chống trùng, snapshot,
priority, PAC/CE/RJ và báo cáo. Judge có `dmoj.tests.test_invocations`, cùng các
test MIN/SUM hiện có. Chạy Django test trên database test riêng; không trỏ
test runner vào database ứng dụng.
