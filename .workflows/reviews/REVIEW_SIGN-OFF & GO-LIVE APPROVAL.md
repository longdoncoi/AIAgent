# REVIEW_SIGN-OFF & GO-LIVE APPROVAL.md — Biên Bản Nghiệm Thu & Phê Chuẩn Go-Live Cấp Quản Lý

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Governance Domain 04 / 04)**  
> **Đơn vị phụ trách**: Ban Đánh Giá Phát Hành (Release Board)  
> **Tài liệu tham chiếu**: [REVIEW.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW.md) · [Architecture.md](file:///f:/PROJECTS/AIAgent/Architecture.md) · [AGENTS.md](file:///f:/PROJECTS/AIAgent/AGENTS.md)

---

## 1. Mục Đích & Thẩm Quyền Phê Duyệt

Biên bản này là văn bản nghiệm thu chính thức cấp quản lý ghi nhận kết quả thẩm định độ sẵn sàng vận hành Production của dự án `agentic-platform`. Văn bản này có giá trị ràng buộc trách nhiệm kỹ thuật và vận hành đối với tất cả các bên tham gia ký kết trước khi mở cổng lưu lượng người dùng thực tế.

---

## 2. Bảng Tổng Kết Đánh Giá Sẵn Sàng (Overall Assessment Summary)

| Chỉ Số Đánh Giá | Kết Quả Thẩm Định | Ngưỡng Tối Thiểu Go-Live | Đánh Giá (Pass / Fail) |
| :--- | :---: | :---: | :---: |
| **Tổng số tiêu chí kiểm tra (14 Trụ cột)** | **73** | **73** | **100% Khảo Sát** |
| **Số tiêu chí P0 đạt (Blocker)** | `____ / 44` | **100% (44/44)** | [ ] Đạt &nbsp; [ ] Không Đạt |
| **Số tiêu chí P1 đạt (Critical)** | `____ / 27` | **$\ge 90\%$ (25/27)** | [ ] Đạt &nbsp; [ ] Không Đạt |
| **Số tiêu chí P2/P3 đạt (Major/Minor)** | `____ / 2` | Kèm kế hoạch Sprint 1 | [ ] Đạt &nbsp; [ ] Không Đạt |
| **Điểm sẵn sàng tổng thể (Readiness Score)** | `____ %` | **$\ge 95.0\%$** | [ ] Đạt &nbsp; [ ] Không Đạt |

---

## 3. Quyết Định Cổng Phát Hành (Production Release Gate Decision)

Hội đồng thẩm định thống nhất đánh dấu lựa chọn một trong ba quyết định dưới đây:

- [ ] **CHẤP THUẬN PHÁT HÀNH (GO)**: Tất cả 44 tiêu chí P0 và $\ge 90\%$ tiêu chí P1 đều đạt. Hệ thống hoàn toàn sẵn sàng tiếp nhận lưu lượng người dùng thực tế trên môi trường Production.
- [ ] **CHẤP THUẬN CÓ ĐIỀU KIỆN (GO WITH CONDITIONS / WAIVER)**: 100% tiêu chí P0 đạt; một số tiêu chí P1 chưa đạt nhưng đã có văn bản cam kết giải pháp thay thế và kế hoạch khắc phục trong 7 ngày được Lead Architect phê chuẩn.
- [ ] **TỪ CHỐI PHÁT HÀNH (NO-GO)**: Tồn tại ít nhất 1 tiêu chí P0 không đạt, hoặc điểm tổng thể $< 95\%$. Hệ thống cần khắc phục triệt để và lên lịch tái thẩm định.

---

## 4. Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Release Board)

| Đại Diện Bộ Phận | Họ Tên Thành Viên | Quyết Định Đánh Giá | Chữ Ký & Ngày Xác Nhận |
| :--- | :--- | :--- | :--- |
| **Chief AI Architect** | ____________________ | [ ] Chấp thuận &nbsp; [ ] Từ chối | ____________________ |
| **Backend / AI Tech Lead** | ____________________ | [ ] Chấp thuận &nbsp; [ ] Từ chối | ____________________ |
| **QA / Test Engineering Lead** | ____________________ | [ ] Chấp thuận &nbsp; [ ] Từ chối | ____________________ |
| **SecOps / Security Lead** | ____________________ | [ ] Chấp thuận &nbsp; [ ] Từ chối | ____________________ |
| **Legal Counsel / DPO** | ____________________ | [ ] Chấp thuận &nbsp; [ ] Từ chối | ____________________ |
| **FinOps / Cloud Cost Lead** | ____________________ | [ ] Chấp thuận &nbsp; [ ] Từ chối | ____________________ |
| **Data Governance Lead** | ____________________ | [ ] Chấp thuận &nbsp; [ ] Từ chối | ____________________ |
| **Principal SRE / DevOps** | ____________________ | [ ] Chấp thuận &nbsp; [ ] Từ chối | ____________________ |
| **Product Owner** | ____________________ | [ ] Chấp thuận &nbsp; [ ] Từ chối | ____________________ |

---

## 5. Điều Khoản Thi Hành & Lưu Trữ Hồ Sơ

1. Hội đồng đối soát và ký đúng source commit `C` cùng release tag dự kiến `vX.Y.Z-prod`; trường `commit_sha` trong biên bản máy đọc được phải là SHA đầy đủ của `C`.
2. Sau khi đủ 9 chữ ký, DevOps tạo commit attestation `A` chỉ thêm/sửa `evidence/signoff/release-board.json`. Không được gộp code, manifest, dependency hoặc tài liệu khác vào `A`.
3. Release tag `vX.Y.Z-prod` chỉ được tạo trỏ vào `A`. CI bắt buộc xác minh `parent(A)=C`, diff `C..A` chỉ có đúng tệp biên bản, `release_tag` khớp tag đang chạy và đủ 9 chữ ký hợp lệ.
4. PRR evidence của `A` phải được tải từ artifact `prr-evidence-<SHA-A>` của chính workflow hiện tại. Vì `A` chỉ chứa biên bản, executable source vẫn đúng bằng `C` đã được Hội đồng ký.
5. Nếu có bất kỳ thay đổi nào ngoài tệp biên bản sau `C`, release hiện tại bị hủy; phải tạo source commit mới, chạy lại toàn bộ gate và lấy lại chữ ký. Biên bản đã ký được lưu trữ vĩnh viễn trong hồ sơ phát hành.

---

## 6. Bằng Chứng Kiểm Định Tự Động (Auto-Evidence Status)

> **AUTO-EVIDENCE PENDING** — chờ đối soát và **ký bởi Release Board**. Không có nhãn `PASSED` được tự động gán cho tài liệu này.

- Artifact: [evidence/prr/latest.json](file:///f:/PROJECTS/AIAgent/evidence/prr/latest.json) (`commit_sha`, `timestamp_utc`, exit code của `ruff` / `mypy` / `pytest --collect-only` / `eval --mode smoke` / parse YAML). Khi phát hành, nguồn chuẩn là artifact CI của commit attestation `A`, không phải bản tracked có thể đã cũ trong checkout.
- Sinh artifact: `python scripts/generate_prr_evidence.py`. Tất cả ô trạng thái `[]` ở các mục trên vẫn giữ nguyên cho đến khi Hội đồng đối soát artifact và ký duyệt.
- Bảng chữ ký 9 đại diện: [REVIEW.md mục 6.3](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).
