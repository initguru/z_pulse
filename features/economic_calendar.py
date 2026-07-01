#!/usr/bin/env python3
"""
미국 경제지표 발표 일정 수집 및 DB 저장
Trading Economics와 웹 스크래핑을 통한 데이터 수집
"""

import asyncio
import logging
import sqlite3


from datetime import datetime, timedelta, date, timezone
import random
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
from bs4 import BeautifulSoup


from z_pulse.utils.markdown_utils import escape_markdown

# 로깅 설정
# logging.basicConfig(
#     level=logging.INFO,
#     format='%(asctime)s - %(levelname)s - %(message)s',
#     handlers=[
#         logging.FileHandler('economic_calendar.log', encoding='utf-8'),
#         logging.StreamHandler()
#     ]
# )
logger = logging.getLogger(__name__)

@dataclass
class EconomicEvent:
    """경제지표 이벤트 데이터 클래스"""
    date: str
    time: str
    country: str
    event_name: str
    importance: str
    actual: Optional[str] = None
    forecast: Optional[str] = None
    previous: Optional[str] = None
    currency: str = "USD"
    source: str = ""

class EconomicCalendarDB:
    """경제지표 캘린더 데이터베이스 관리 클래스"""
    
    def __init__(self, db_path: str = "economic_calendar.db"):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """데이터베이스 및 테이블 초기화"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS economic_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        date TEXT NOT NULL,
                        time TEXT,
                        country TEXT NOT NULL,
                        event_name TEXT NOT NULL,
                        importance TEXT,
                        actual TEXT,
                        forecast TEXT,
                        previous TEXT,
                        currency TEXT DEFAULT 'USD',
                        source TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(date, time, event_name, country)
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS economic_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # 인덱스 생성
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_date ON economic_events(date)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_country ON economic_events(country)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_importance ON economic_events(importance)')

                conn.commit()
                logger.info("데이터베이스 초기화 완료")
        except Exception as e:
            logger.error(f"데이터베이스 초기화 실패: {e}")
            raise

    def set_meta(self, key: str, value: str):
        """메타데이터 저장"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO economic_meta (key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = CURRENT_TIMESTAMP
                ''', (key, value))
                conn.commit()
        except Exception as e:
            logger.error(f"메타데이터 저장 실패 ({key}): {e}")
            raise

    def get_meta(self, key: str, default: str = '') -> str:
        """메타데이터 조회"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT value FROM economic_meta WHERE key = ?', (key,))
                result = cursor.fetchone()
                return result[0] if result is not None and result[0] is not None else default
        except Exception as e:
            logger.error(f"메타데이터 조회 실패 ({key}): {e}")
            return default

    def insert_events(self, events: List[EconomicEvent]) -> int:
        """경제지표 이벤트들을 데이터베이스에 삽입"""
        inserted_count = 0
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                for event in events:
                    try:
                        cursor.execute('''
                            INSERT OR REPLACE INTO economic_events 
                            (date, time, country, event_name, importance, actual, forecast, previous, currency, source, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ''', (
                            event.date, event.time, event.country, event.event_name,
                            event.importance, event.actual, event.forecast, event.previous,
                            event.currency, event.source
                        ))
                        inserted_count += 1
                    except sqlite3.Error as e:
                        logger.warning(f"이벤트 삽입 실패: {event.event_name} - {e}")
                        continue
                
                conn.commit()
                logger.info(f"{inserted_count}개 이벤트 저장 완료")
                
        except Exception as e:
            logger.error(f"데이터베이스 삽입 실패: {e}")
            raise
        
        return inserted_count
    
    def get_events_by_date_range(self, start_date: str, end_date: str) -> List[Dict]:
        """날짜 범위로 이벤트 조회"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT * FROM economic_events 
                    WHERE date BETWEEN ? AND ? 
                    ORDER BY date, time
                ''', (start_date, end_date))
                
                columns = [description[0] for description in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
                
        except Exception as e:
            logger.error(f"데이터 조회 실패: {e}")
            return []

class EconomicCalendarScraper:
    """경제지표 캘린더 스크래퍼 (차단 회피 버전)"""

    # User-Agent 로테이션용 리스트
    USER_AGENTS = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
    ]

    def __init__(self, db: Optional['EconomicCalendarDB'] = None):
        # self.session = requests.Session()
        # self.db = db  # 스마트 캐싱용
        # self.retry_count = 0
        # self._rotate_headers()

        # 비동기 HTTP 클라이언트
        self._client = None  # httpx.AsyncClient는 필요 시 생성
        self._current_headers = {}
        self.db = db  # 스마트 캐싱용
        self.retry_count = 0
        self._rotate_headers()

    def _rotate_headers(self):
        """
        User-Agent 로테이션 및 헤더 준비

        Note: investing.com은 복잡한 헤더(Sec-Ch-*, Sec-Fetch-* 등)를 사용하면
        오히려 봇으로 감지합니다. 단순한 헤더가 더 효과적입니다.
        """
        # [Phase 1.5] 헤더를 딕셔너리로 저장 (httpx 클라이언트에 전달용)
        self._current_headers = {
            'User-Agent': random.choice(self.USER_AGENTS),
            'Content-Type': 'application/x-www-form-urlencoded',
        }

    async def _get_client(self):
        """
        httpx AsyncClient 반환 (재사용)

        매번 새로 생성하지 않고 재사용하되, 닫힌 경우 재생성
        """
        import httpx  # lazy import to avoid MemoryError in multiprocessing spawn
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
                headers=self._current_headers,
                follow_redirects=True
            )
        return self._client

    async def close(self):
        """리소스 정리"""
        if self._client:
            await self._client.aclose()

    def _get_delay(self) -> float:
        """요청 간격 계산 (45~75초 기본, 지수 백오프)"""
        base_delay = random.uniform(45, 75)
        if self.retry_count > 0:
            base_delay *= (2 ** min(self.retry_count, 3))  # 최대 8배
        return base_delay

    def _should_fetch_date(self, date_str: str) -> bool:
        """스마트 캐싱: DB에 데이터 있고 과거면 스킵"""
        if self.db is None:
            return True
        today = datetime.now().strftime('%Y-%m-%d')
        if date_str < today:
            existing = self.db.get_events_by_date_range(date_str, date_str)
            if existing:
                logger.info(f"⏭️ {date_str}: 캐시 사용 (스킵)")
                return False
        return True
    
    async def scrape_investing_com(self, days_ahead: int = 7) -> List[EconomicEvent]:
        """
        비동기 스크래핑 - Investing.com 경제지표 캘린더

        기존: requests.post() 동기 블로킹 (최대 30초)
        개선: httpx.AsyncClient 사용하여 이벤트 루프 블로킹 제거

        Note: investing.com의 POST API를 사용하여 날짜 범위를 지정해 데이터를 수집합니다.
        미국(country=5) + 고중요도(importance=3) 이벤트만 필터링하여 요청합니다.
        """
        events = []

        try:
            # 헤더 로테이션
            self._rotate_headers()

            # [Phase 1.5] httpx 클라이언트 획득
            client = await self._get_client()

            # API 요청에 필요한 추가 헤더
            request_headers = {
                **self._current_headers,
                'X-Requested-With': 'XMLHttpRequest',
            }

            api_url = "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData"

            # 날짜 범위 계산 (하루 전부터 - 시간대 차이 보정)
            today = datetime.now().date()
            start_date = (today - timedelta(days=1)).strftime('%Y-%m-%d')
            end_date = (today + timedelta(days=days_ahead)).strftime('%Y-%m-%d')

            # POST 데이터: 미국(5) + 고중요도(3)만 요청
            post_data = {
                'country[]': '5',  # USA
                'importance[]': '3',  # High importance
                'dateFrom': start_date,
                'dateTo': end_date,
            }

            try:
                # [Phase 1.5] 비동기 POST 요청 (timeout은 클라이언트 생성 시 설정됨)
                response = await client.post(
                    api_url,
                    data=post_data,
                    headers=request_headers
                )

                # 429 에러(Too Many Requests) 처리 - 즉시 중단
                if response.status_code == 429:
                    logger.warning("⛔ 429 차단 감지. 수집 중단, 다음 스케줄에서 재시도.")
                    self.retry_count += 1
                    return events

                response.raise_for_status()

                # JSON 응답에서 HTML 데이터 추출
                result = response.json()
                html_data = result.get('data', '')

                if not html_data:
                    logger.warning("경제 캘린더 데이터가 비어있습니다.")
                    return events

                soup = BeautifulSoup(html_data, 'html.parser')
                event_rows = soup.find_all('tr', class_='js-event-item')

                logger.info(f"📊 {len(event_rows)}개 이벤트 행 발견 ({start_date} ~ {end_date})")

                for row in event_rows:
                    try:
                        event = self._parse_event_html(row)
                        if event:
                            # 스마트 캐싱: 이미 DB에 있는 과거 날짜는 스킵
                            if self._should_fetch_date(event.date):
                                events.append(event)
                    except Exception as e:
                        logger.warning(f"이벤트 파싱 실패: {e}")
                        continue

            except Exception as e:
                logger.warning(f"Investing.com 요청 실패: {e}")
                self.retry_count += 1
                return events

        except Exception as e:
            logger.error(f"Investing.com 스크래핑 실패: {e}")

        # 성공 시 retry_count 리셋
        self.retry_count = 0
        logger.info(f"Investing.com에서 총 {len(events)}개 이벤트 수집 완료")
        return events

    def _parse_event_html(self, row) -> Optional[EconomicEvent]:
        """Investing.com HTML 이벤트 행 파싱"""
        try:
            # 날짜/시간 추출 (data-event-datetime 속성: "2026/02/05 08:30:00")
            datetime_attr = row.get('data-event-datetime', '')
            if not datetime_attr:
                return None

            # 날짜와 시간 분리
            parts = datetime_attr.split(' ')
            date_str = parts[0].replace('/', '-')  # "2026/02/05" -> "2026-02-05"
            time_str = parts[1][:5] if len(parts) > 1 else ''  # "08:30:00" -> "08:30"

            # 이벤트 이름 추출
            event_cell = row.find('td', class_='event')
            if not event_cell:
                return None
            event_name = event_cell.get_text(strip=True)

            if not event_name:
                return None

            # 실제/예측/이전 값 추출
            actual = None
            forecast = None
            previous = None

            # actual 값
            actual_cell = row.find('td', class_='act')
            if actual_cell:
                actual = actual_cell.get_text(strip=True) or None

            # forecast 값
            forecast_cell = row.find('td', class_='fore')
            if forecast_cell:
                forecast = forecast_cell.get_text(strip=True) or None

            # previous 값
            prev_cell = row.find('td', class_='prev')
            if prev_cell:
                previous = prev_cell.get_text(strip=True) or None

            return EconomicEvent(
                date=date_str,
                time=time_str,
                country='United States',  # API에서 이미 USA만 필터링
                event_name=event_name.strip(),
                importance='3',  # API에서 이미 고중요도만 필터링
                actual=actual,
                forecast=forecast,
                previous=previous,
                source="investing.com"
            )

        except Exception as e:
            logger.warning(f"HTML 파싱 중 오류: {e}")
            return None


class EconomicCalendarManager:
    """경제지표 캘린더 관리자"""

    def __init__(self, db_path: str = "economic_calendar.db"):
        self.db = EconomicCalendarDB(db_path)
        self.scraper = EconomicCalendarScraper(db=self.db)  # 스마트 캐싱을 위해 db 전달
    
    async def update_calendar(self, days_ahead: int = 7) -> Dict[str, int]:
        """경제지표 캘린더 업데이트"""
        logger.info(f"경제지표 캘린더 업데이트 시작 (앞으로 {days_ahead}일)")

        results = {
            'investing_com': 0,
            'total': 0
        }

        # Investing.com에서 데이터 수집
        try:
            # # [Phase 1.5] 기존 코드 (asyncio.to_thread 우회)
            # # 비동기 실행을 위해 스레드 풀 사용 등을 고려할 수 있으나,
            # # 여기서는 간단히 동기 함수를 호출 (블로킹 발생 가능)
            # # 봇 전체 멈춤 방지를 위해 run_in_executor 사용 권장되나
            # # 현재 구조 유지를 위해 직접 호출 (필요시 리팩토링)
            # investing_events = await asyncio.to_thread(self.scraper.scrape_investing_com, days_ahead)

            # 네이티브 async 호출 (asyncio.to_thread 제거)
            investing_events = await self.scraper.scrape_investing_com(days_ahead)

            if investing_events:
                results['investing_com'] = self.db.insert_events(investing_events)

            results['total'] = results['investing_com']
            self.record_update_status(
                success=True,
                inserted_count=results['total'],
            )
        except Exception as e:
            logger.error(f"Investing.com 데이터 수집 실패: {e}")
            results['total'] = results['investing_com']
            self.record_update_status(
                success=False,
                inserted_count=results['total'],
                error_message=str(e),
            )

        logger.info(f"캘린더 업데이트 완료: {results}")
        return results
    
    def get_upcoming_events(self, days: int = 7) -> List[Dict]:
        """다가오는 경제지표 이벤트 조회"""
        start_date = datetime.now().strftime('%Y-%m-%d')
        end_date = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d')
        
        return self.db.get_events_by_date_range(start_date, end_date)
    
    def get_high_importance_events(self, days: int = 7) -> List[Dict]:
        """고중요도 경제지표 이벤트 조회"""
        events = self.get_upcoming_events(days)
        return [event for event in events if event.get('importance', '1') in ['3', 'High']]

    def record_update_status(self, success: bool, inserted_count: int, error_message: Optional[str] = None):
        """업데이트 상태 메타데이터 기록"""
        attempt_at = datetime.now().isoformat()
        self.db.set_meta('last_attempt_at', attempt_at)
        self.db.set_meta('last_inserted_count', str(inserted_count))

        if success:
            self.db.set_meta('last_success_at', attempt_at)
            self.db.set_meta('last_error', '')
        else:
            self.db.set_meta('last_error', error_message or '')

    def get_status_summary(self) -> Dict[str, object]:
        """업데이트 상태 메타데이터 요약 조회"""
        last_inserted_count = self.db.get_meta('last_inserted_count', '0')
        try:
            inserted_count = int(last_inserted_count)
        except (TypeError, ValueError):
            inserted_count = 0

        return {
            'last_attempt_at': self.db.get_meta('last_attempt_at', ''),
            'last_success_at': self.db.get_meta('last_success_at', ''),
            'last_inserted_count': inserted_count,
            'last_error': self.db.get_meta('last_error', ''),
        }

    def needs_bootstrap_update(self, days: int = 7) -> bool:
        """시작 시 부트스트랩 업데이트가 필요한지 확인"""
        status = self.get_status_summary()
        if not status['last_success_at']:
            return True

        return len(self.get_upcoming_events(days)) == 0

    def build_empty_reason_message(self) -> str:
        """경제지표가 비어 있을 때 상태 진단 메시지 생성"""
        status = self.get_status_summary()
        last_attempt_at = status['last_attempt_at']
        last_success_at = status['last_success_at']
        last_error = status['last_error']

        if not last_attempt_at:
            return "초기 수집 전입니다. 아직 경제지표 캘린더를 한 번도 업데이트하지 않았습니다."

        reasons = []

        if not last_success_at:
            reasons.append("초기 수집이 아직 완료되지 않았습니다.")

        if last_error:
            reasons.append(f"최근 오류: {last_error}")

        if not reasons:
            reasons.append("최근 업데이트에서 표시할 경제지표가 없었습니다.")

        return "\n".join(reasons)

    # ========== 새로운 유틸리티 메서드 ==========

    @staticmethod
    def convert_to_korea_time(date_str: str, time_str: str) -> Tuple[date, str]:
        """
        미국 동부시간(ET)을 한국시간(KST)으로 변환

        Note: investing.com POST API는 미국 동부시간(ET) 기준입니다.
        ET는 UTC-5 (겨울, EST) 또는 UTC-4 (서머타임, EDT)입니다.

        Args:
            date_str: YYYY-MM-DD 형식의 날짜 문자열
            time_str: HH:MM 형식의 시간 문자열 (빈 문자열 가능)

        Returns:
            (korea_date, korea_time_str) 튜플
            korea_date: datetime.date 객체
            korea_time_str: HH:MM 형식의 문자열 (시간 정보가 없으면 빈 문자열)
        """
        korea_offset = timezone(timedelta(hours=9))
        # 미국 동부시간 (겨울: UTC-5, 서머타임: UTC-4)
        # 간단히 UTC-5로 가정 (대부분의 경제지표는 겨울 시간 기준으로도 충분)
        # 정확한 DST 처리가 필요하면 zoneinfo 사용 권장
        et_offset = timezone(timedelta(hours=-5))

        try:
            if time_str and ':' in time_str:
                hour, minute = map(int, time_str.split(':'))
                # 미국 동부시간으로 파싱
                et_datetime = datetime.strptime(f"{date_str} {hour:02d}:{minute:02d}", '%Y-%m-%d %H:%M')
                et_datetime = et_datetime.replace(tzinfo=et_offset)

                # 한국시간으로 변환
                korea_datetime = et_datetime.astimezone(korea_offset)
                korea_date = korea_datetime.date()
                korea_time = korea_datetime.strftime('%H:%M')
                return korea_date, korea_time
            else:
                # 시간 정보가 없는 경우
                event_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                return event_date, ""
        except Exception as e:
            logger.warning(f"시간 변환 실패 ({date_str} {time_str}): {e}")
            # 실패 시 원본 날짜와 시간 반환 (변환 없음)
            event_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            return event_date, time_str

    @staticmethod
    def get_date_display(target_date: date) -> str:
        """
        날짜를 '오늘'/'내일'/'모레'/MM/DD 형식으로 변환

        Args:
            target_date: 변환할 날짜 (datetime.date 객체)

        Returns:
            표시용 날짜 문자열
        """
        today = datetime.now().date()
        if target_date == today:
            return "오늘"
        elif target_date == today + timedelta(days=1):
            return "내일"
        elif target_date == today + timedelta(days=2):
            return "모레"
        else:
            return target_date.strftime('%m/%d')

    def format_events_message(self, days: int = 7, max_events: int = 8,
                              last_update: Optional[datetime] = None) -> Optional[str]:
        """
        경제지표 목록을 MarkdownV2 형식 메시지로 포맷팅

        Args:
            days: 조회할 일수 (기본값 7)
            max_events: 최대 표시 이벤트 수 (기본값 8)
            last_update: 마지막 업데이트 시간 (선택)

        Returns:
            포맷팅된 메시지 문자열 (이벤트가 없으면 None)
        """
        upcoming_events = self.get_upcoming_events(days)
        high_importance_events = self.get_high_importance_events(days)
        
        if not upcoming_events:
            return None
        
        # 메시지 구성 (MarkdownV2)
        message = "💰 *미국 경제지표 캘린더*\n\n"
        message += f"📊 총 {len(upcoming_events)}개 이벤트 \\| 🔴 고중요도 {len(high_importance_events)}개\n\n"
        
        # 고중요도 이벤트만 먼저 표시
        if high_importance_events:
            message += "🔴 *고중요도 이벤트*\n"
            
            for event in high_importance_events[:max_events]:
                date_str = event['date']
                time_str = event['time'] or ""
                
                korea_date, korea_time = self.convert_to_korea_time(date_str, time_str)
                date_display = self.get_date_display(korea_date)
                time_display = korea_time if korea_time else ""
                
                esc_date = escape_markdown(date_display)
                esc_time = escape_markdown(time_display)
                esc_name = escape_markdown(event['event_name'])
                
                if time_display:
                    message += f"📅 {esc_date} {esc_time} \\| {esc_name}\n"
                else:
                    message += f"📅 {esc_date} \\| {esc_name}\n"
            
            message += "\n"
        
        # 전체 이벤트 요약 (오늘, 내일 개수) - 한국시간 기준으로 카운팅
        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)

        today_count = 0
        tomorrow_count = 0
        for e in upcoming_events:
            korea_date, _ = self.convert_to_korea_time(e['date'], e.get('time', ''))
            if korea_date == today:
                today_count += 1
            elif korea_date == tomorrow:
                tomorrow_count += 1
        
        message += "📈 *일정 요약*\n"
        message += f"• 오늘: {today_count}개 이벤트\n"
        message += f"• 내일: {tomorrow_count}개 이벤트\n\n"
        
        if last_update:
            update_time = escape_markdown(last_update.strftime('%m/%d %H:%M'))
            message += f"🔄 마지막 업데이트: {update_time}"
        
        return message

    def format_alert_message(self, events: List[Dict], max_events: int = 5) -> str:
        """
        고중요도 이벤트 알림 메시지 포맷팅

        Args:
            events: EconomicEvent 딕셔너리 리스트
            max_events: 최대 표시 이벤트 수 (기본값 5)

        Returns:
            포맷팅된 알림 메시지 문자열
        """
        if not events:
            return "⚠️ 고중요도 이벤트가 없습니다."
        
        message = "🔴 *다가오는 고중요도 경제지표*\n\n"
        
        for event in events[:max_events]:
            date_str = event['date']
            time_str = event.get('time', '')
            
            korea_date, korea_time = self.convert_to_korea_time(date_str, time_str)
            date_display = self.get_date_display(korea_date)
            time_display = korea_time if korea_time else ""
            
            esc_date = escape_markdown(date_display)
            esc_time = escape_markdown(time_display)
            esc_name = escape_markdown(event['event_name'])
            
            if time_display:
                message += f"📅 {esc_date} {esc_time} \\| {esc_name}\n"
            else:
                message += f"📅 {esc_date} \\| {esc_name}\n"
        
        message += "💡 경제지표 발표는 시장 변동성을 증가시킬 수 있습니다\\."
        return message

    def should_update(self, last_update: Optional[datetime]) -> bool:
        """
        캘린더 업데이트 필요 여부 확인 (24시간 경과 시 True)

        Args:
            last_update: 마지막 업데이트 시간

        Returns:
            업데이트 필요 여부
        """
        if last_update is None:
            return True

        now = datetime.now()
        elapsed_hours = (now - last_update).total_seconds() / 3600
        return elapsed_hours >= 24

    async def close(self):
        """
        리소스 정리

        httpx.AsyncClient를 안전하게 닫아 리소스 누수 방지
        """
        await self.scraper.close()
        logger.info("Economic calendar resources cleaned up")


async def main():
    """메인 실행 함수"""
    manager = EconomicCalendarManager()
    try:
        # 캘린더 업데이트
        results = await manager.update_calendar(days_ahead=14)
        print(f"업데이트 결과: {results}")

        # 다가오는 고중요도 이벤트 조회
        high_importance_events = manager.get_high_importance_events(7)

        print(f"\n다가오는 고중요도 경제지표 ({len(high_importance_events)}개):")
        for event in high_importance_events[:10]:  # 상위 10개만 출력
            print(f"- {event['date']} {event['time']} | {event['event_name']} | 중요도: {event['importance']}")

    except Exception as e:
        logger.error(f"메인 실행 중 오류: {e}")
        raise
    finally:
        await manager.close()

if __name__ == "__main__":
    asyncio.run(main())