# Triển khai judge với --no-watchdog

Mặc định chạy mọi judge ClueOJ với **`--no-watchdog`**, cả local và remote.
Đây là cờ runtime đã có trong source, không cần một source hay image riêng tên
`nowatch`. Chỉ thay cờ khởi động thì không cần build lại image.

Cờ này tắt watchdog theo dõi filesystem của judge. Judge vẫn có thể quét lúc
khởi động và khi nhận `POST /update/problems`; không thêm `--skip-first-scan`
mặc định. Kiểu local/remote trong Django admin (`is_remote`) là cấu hình riêng.

Khi thư mục problems ghi được, đường quét hiện tại ghi lại `cache.pkl`, trong
khi watchdog của judge cũng theo dõi file đó. Điều này có thể tự kích hoạt quét
và gửi danh sách bài nhiều lần. Storage lọc file ở thư mục gốc như `cache.pkl`,
gom sự kiện rồi gọi API judge, nên dùng storage làm nơi phát thông báo cập nhật.

## Judge dùng chung filesystem với OJ và storage

Trước khi chuyển, xác nhận storage đang theo dõi đúng thư mục problems và gọi
được control API của từng judge. Trong cấu hình external-storage:

```dotenv
CLUEOJ_SITE_NETWORK=clueoj_site
STORAGE_JUDGE_UPDATE_URLS=http://judge:9001/update/problems,http://judge02:9001/update/problems,http://judge03:9001/update/problems
```

Thay network và tên service theo deployment thực tế. Các URL trên dành cho
judge cùng nhìn thấy filesystem problems đó. Storage phải tham gia mạng Docker
tương ứng, hoặc có kết nối private tới các judge. Chỉ cấu hình notifier đang có;
không cần sửa source external-storage.

Giữ `-A 0.0.0.0 -a 9001` để nhận thông báo trong mạng container. Control API
không có xác thực; không publish cổng 9001 ra Internet. `0.0.0.0` ở đây là địa
chỉ bên trong container, không tự tạo host port.

Lệnh chạy với entrypoint production của judge:

```yaml
command: run --no-watchdog -p 9999 -c /problems/judge01.yml bridged -A 0.0.0.0 -a 9001
```

`judge02`/`judge03` dùng file cấu hình riêng tương ứng. Giữ nguyên image, runtime,
authentication và đường mount problems đang dùng. Chờ lượt đang chấm kết thúc,
rồi recreate từng judge, chẳng hạn từ thư mục `clueoj`:

```sh
docker compose -f docker-compose.yml -f docker-compose.storage-client.yml --profile judge up -d --no-deps --no-build --force-recreate judge
```

Lặp lại cho `judge02`, `judge03` sau khi xác nhận máy trước đã kết nối lại.
Nếu deployment dùng thêm Compose override, giữ nguyên các file đó trong lệnh.
Một lệnh `restart` không áp dụng thay đổi `command`; phải recreate container.

Kiểm tra sau triển khai:

1. Command của container có `--no-watchdog`; judge online, đúng local/remote và
   đủ runtimes như trước. Cổng 9001 chỉ có thể truy cập qua mạng private.
2. Tạo một bài thử riêng; sau thay đổi, storage gửi `POST /update/problems`,
   judge báo cập nhật danh sách và nhận được bài thử. API trả 200 chỉ xác nhận
   đã nhận yêu cầu, nên chờ tới khi danh sách bài cập nhật thực sự.
3. Nộp AC/WA, kiểm tra mirror và Unlock → AB → Apply → rejudge. Lượt đã AB vẫn
   giữ AB; bài đã chấm xong chỉ thay kết quả khi rejudge rõ ràng.
4. Quan sát log khi không chỉnh test: không có chuỗi cập nhật liên tục do
   `cache.pkl`. Ping trên OJ gồm độ trễ xử lý ở hai đầu; so sánh dưới cùng tải
   trước khi kết luận mức cải thiện.

Notifier của storage báo thay đổi filesystem; thông báo này không chứng minh
file đã upload xong lên R2. Remote judging vẫn phải dùng manifest/readiness và
kiểm tra SHA-256 như trong [remote-judges.md](remote-judges.md).

## Local chưa bật external-storage

`compose.local.yml` chạy image source mới với `--no-watchdog`, giữ
`localjudge.is_remote=False`. Local hiện chưa bật external-storage; sau khi
Apply test hoặc thêm/xóa/chuyển nguồn bài thử, chủ động refresh qua API private:

```sh
./scripts/local exec -T site python3 - <<'PY'
import urllib.request
request = urllib.request.Request('http://judge:9001/update/problems', data=b'')
with urllib.request.urlopen(request, timeout=5) as response:
    print(response.status)
PY
```

Sau khi judge rảnh, áp dụng thay đổi command bằng:

```sh
./scripts/local up -d --no-deps --no-build --force-recreate judge
./scripts/local logs --tail=60 judge
```

Entry point local gọi `dmoj` trực tiếp, nên command local bắt đầu bằng
`bridged --no-watchdog ...`, không thêm từ `run` của image production.
Không cần bật R2 hoặc dựng thêm storage để kiểm tra chế độ này trên local.

## Remote dùng cache riêng

Remote cũng chạy `--no-watchdog`. Cache được chuẩn bị theo manifest của từng
lượt; không cần đưa control URL của máy remote vào notifier theo dõi filesystem
VDS mà máy đó không dùng chung. Giữ các cấu hình cache và xác minh phiên bản
trong [remote-judges.md](remote-judges.md); cờ watchdog không thay thế chúng.

## Docker run pin CPU/NUMA: local và remote

`--no-watchdog` nằm sau image và từ `run`, là tham số của judge. Các cờ
`--cpuset-cpus`, `--cpuset-mems`, `-m`, `--memory-swap` nằm trước image, là tham
số Docker. Ví dụ dưới giữ CPU 7, NUMA node 1 và giới hạn RAM/swap như lệnh
judge04; CPU và NUMA node phải tồn tại và phù hợp trên host triển khai.

### Local trên cùng host với bridge

Giữ `is_remote=False` trong Django admin. Image trong ví dụ là tag bạn đang
dùng; khi cập nhật source judge thì thay bằng image đã build tương ứng.

```sh
sudo docker run \
  --name judge04 \
  --network=host \
  -v /home/dmoj/vnoj-docker/dmoj/problems:/problems \
  --cap-add=SYS_PTRACE \
  --cpuset-cpus="7" \
  --cpuset-mems="1" \
  -m 2g \
  --memory-swap 2g \
  -d \
  --restart=always \
  clueoj/judge:min-batch-v1 \
  run --no-watchdog -p 9999 -c /problems/judge04.yml 127.0.0.1 -A 0.0.0.0 -a 9014
```

`127.0.0.1` là đích bridge khi bridge ở cùng host và publish port 9999.
`0.0.0.0` dùng làm địa chỉ listen (`-A`), không làm địa chỉ đích cho bridge.
Với `--network=host`, API 9014 lắng nghe trực tiếp trên host: chỉ cho storage
truy cập qua mạng private/firewall. Thêm URL
`http://PRIVATE_HOST_IP:9014/update/problems` vào `STORAGE_JUDGE_UPDATE_URLS`;
không dùng DNS service `judge04` của Docker bridge cho container host-network.

### Remote trên server khác

Dùng image build từ source judge mới có capability `problem-data-manifest-v1`;
không mặc định tag `min-batch-v1` cũ đã có tính năng remote. Hai máy có thể chạy
cùng một image source mới. Đặt `JUDGE_IMAGE` thành image đó và
`VDS_BRIDGE_HOST` thành IP/VPN hostname private của VDS trước khi chạy:

```sh
sudo docker run \
  --name remote-judge04 \
  --network=host \
  -v /home/dmoj/remote-judge/judge04.yml:/problems/judge04.yml:ro \
  -v /home/dmoj/remote-judge/cache:/var/cache/clueoj/problem-data \
  --cap-add=SYS_PTRACE \
  --cpuset-cpus="7" \
  --cpuset-mems="1" \
  -m 2g \
  --memory-swap 2g \
  -d \
  --restart=always \
  "${JUDGE_IMAGE:?Set image built from the updated shared judge source}" \
  run --no-watchdog -p 9999 -c /problems/judge04.yml \
  "${VDS_BRIDGE_HOST:?Set private VDS bridge host}" -A 127.0.0.1 -a 9014
```

File cấu hình remote giữ ID/key/runtime riêng và có:

```yaml
remote_data_cache: /var/cache/clueoj/problem-data
remote_data_timeout: 1800
remote_data_allow_http: false
```

Tạo sẵn file cấu hình và thư mục cache trên host; cache phải ghi được bởi user
judge trong image. Remote không mount bộ test canonical của VDS. Control API
remote bind loopback vì chuẩn bị dữ liệu theo từng lượt không cần notifier của
storage trên VDS gọi vào máy remote. Đánh dấu judge này `is_remote=True` khi
offline, rồi kết nối lại; chỉ bật nhận bài remote sau các bước manifest/readiness
trong [remote-judges.md](remote-judges.md).

Khi thay command của container đã có, chờ lượt chấm kết thúc, stop/remove đúng
container cũ rồi tạo lại với lệnh tương ứng; `docker restart` không sửa command.
Hai lệnh trên là mẫu deployment Linux, không chạy thêm judge04 trên môi trường
Docker Desktop local đã kiểm thử ở mục trước.

## Khôi phục khi notifier gặp sự cố

Giữ `--no-watchdog`, sửa kết nối/cấu hình notifier và gọi API refresh thủ công
cho các judge dùng chung filesystem. Có thể recreate judge để quét lại lúc
khởi động. Không bật lại watchdog như cách khôi phục mặc định vì có thể tái
tạo vòng cập nhật cache; không bỏ qua kiểm tra manifest của remote.
