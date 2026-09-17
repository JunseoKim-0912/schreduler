"""send_push_notification이 (아직 Firebase 자격증명도, 실제 디바이스 토큰도
없는 상태에서) 예외 없이 로그를 남기고 끝나는지 수동으로 확인하는 스크립트.

실행: python check_fcm.py
"""

import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from app.services.notification import send_push_notification

send_push_notification(
    device_token="fake-device-token-for-manual-testing",
    title="테스트 알림",
    body="이 메시지가 에러 없이 로그로 남으면 정상입니다.",
)

print("완료 — 위에 에러 트레이스백 없이 로그 한 줄만 찍혔으면 정상입니다.")
