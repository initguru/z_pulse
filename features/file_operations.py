"""
File Operations: 파일 전송 및 관리

Z-Pulse에서 파일 작업 관련 코드 분리
"""

import asyncio
import logging
import os
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union, List

from telegram import Update, CallbackQuery

from z_pulse.utils.formatters import format_log_caption, strip_ansi
from z_pulse.utils.markdown_utils import escape_markdown

if TYPE_CHECKING:
    from z_pulse.features.process_control import ProcessController

logger = logging.getLogger(__name__)


class FileOperations:
    """파일 전송 및 관리를 담당하는 클래스"""

    def __init__(self, process_controller: "ProcessController"):
        """
        Args:
            process_controller: ProcessController 인스턴스 (find_target_directory 접근용)
        """
        self.process_controller = process_controller

    def _resolve_log_file_path(self, target_path: Path) -> Optional[Path]:
        """대상 경로에 맞는 로그 파일 경로를 반환합니다."""
        try:
            if target_path.exists() and target_path.is_file():
                base_dir = target_path.parent
            elif target_path.exists() and target_path.is_dir():
                base_dir = target_path
            else:
                base_dir = target_path.parent if target_path.suffix else target_path
        except (OSError, TypeError, ValueError):
            base_dir = target_path.parent if target_path.suffix else target_path
        candidates = [base_dir / "monitor.log"]

        for candidate in candidates:
            if (
                candidate.exists()
                and candidate.is_file()
                and candidate.stat().st_size > 0
            ):
                return candidate

        return None

    async def send_file_async(
        self,
        update_or_query: Union[Update, CallbackQuery],
        file_path: str,
        caption: str,
        file_type: str = "document",
        filename: Optional[str] = None,
        target_name: Optional[str] = None,  # [추가] 대상 이름 (예: STANDX-MM)
    ) -> bool:
        """
        파일을 비동기적으로 전송 (MarkdownV2 적용)

        Args:
            update_or_query: Update 또는 CallbackQuery 객체
            file_path: 전송할 파일의 경로
            caption: 파일과 함께 보낼 캡션 (MarkdownV2 이스케이프 필요)
            file_type: 'document' 또는 'photo'
            filename: 전송 시 사용할 파일 이름 (None이면 원래 이름)
            target_name: 메시지에 표시할 대상 이름 (옵션)

        Returns:
            성공 여부
        """
        if not os.path.exists(file_path):
            logger.error(f"파일 전송 실패: 파일을 찾을 수 없음 ({file_path})")
            return False

        # 적절한 reply 객체 선택
        reply_obj = getattr(update_or_query, "message", None)

        if not reply_obj:
            logger.error("잘못된 update_or_query 객체 타입")
            return False

        # 사용할 파일 이름 결정
        display_filename = filename if filename else os.path.basename(file_path)

        # 대기 메시지 전송
        waiting_msg = None
        task_name = "스크린샷" if file_type == "photo" else "파일"

        # [수정] 대기 메시지에 대상 이름 포함
        if target_name:
            msg_text = f"⏳ *[{target_name}]* {task_name} 전송을 준비 중입니다..."
        else:
            msg_text = f"⏳ {task_name} 전송을 준비 중입니다..."

        try:
            # MarkdownV2가 아닐 수도 있으므로 안전하게 일반 텍스트로 보낼 수도 있지만,
            # 여기서는 편의상 일반 텍스트로 보냅니다 (이스케이프 복잡성 회피)
            waiting_msg = await reply_obj.reply_text(
                msg_text.replace("*", "").replace("[", "").replace("]", "")
            )
        except Exception as e:
            logger.warning(f"대기 메시지 전송 실패: {e}")

        try:
            # 호출자가 이미 MarkdownV2 포맷팅/이스케이프를 했다고 가정
            safe_caption = caption

            with open(file_path, "rb") as f:
                if file_type == "photo":
                    await reply_obj.reply_photo(
                        photo=f,
                        caption=safe_caption,
                        parse_mode="MarkdownV2",
                        read_timeout=120,
                        write_timeout=120,
                    )
                else:  # 기본값은 document
                    await reply_obj.reply_document(
                        document=f,
                        filename=display_filename,
                        caption=safe_caption,
                        parse_mode="MarkdownV2",
                        read_timeout=120,
                        write_timeout=120,
                    )

            logger.info(f"{file_type.capitalize()} 전송 성공: {display_filename}")
            return True
        except Exception as e:
            logger.error(f"파일 전송 중 오류 ({file_path}): {e}")
            # 사용자에게도 오류 알림
            if reply_obj:
                try:
                    await reply_obj.reply_text(
                        f"❌ 파일({os.path.basename(file_path)}) 전송에 실패했습니다."
                    )
                except:
                    pass
            return False
        finally:
            # 대기 메시지 삭제
            if waiting_msg:
                try:
                    await waiting_msg.delete()
                except Exception as e:
                    logger.warning(f"대기 메시지 삭제 실패: {e}")

    async def send_log_helper(
        self,
        update_or_query: Union[Update, CallbackQuery],
        target_path: Path,
        tail: Optional[int] = None,
    ) -> bool:
        """[공통] 로그 전송 헬퍼 메서드"""
        try:
            dir_name = target_path.parent.name
            log_file_path = self._resolve_log_file_path(target_path)

            if log_file_path is None:
                return False

            if tail:
                # [최적화] Tail 읽기 - run_in_executor로 비동기 처리
                def _read_tail():
                    with open(
                        log_file_path, "r", encoding="utf-8", errors="ignore"
                    ) as f:
                        lines = deque(f, tail)
                    return "".join(lines)

                loop = asyncio.get_running_loop()
                content = await loop.run_in_executor(None, _read_tail)

                # ANSI 이스케이프 시퀀스 제거 (텔레그램은 지원하지 않음)
                content = strip_ansi(content)

                if not content.strip():
                    return False

                # 메시지 포맷팅 통합
                header = format_log_caption(dir_name, tail_lines=tail)

                # 텔레그램 메시지 길이 제한 (4096자)
                # 헤더 + 코드블록 마커(``` x 2 + \n x 2) 여유분 고려
                max_content_len = 4096 - len(header) - 10

                if len(content) > max_content_len:
                    # 마지막 부분만 잘라서 전송 (줄 단위로 자르기)
                    truncated = content[-(max_content_len - 20) :]  # 말줄임 표시 여유분
                    # 첫 번째 줄이 잘렸을 수 있으므로 다음 줄부터 시작
                    first_newline = truncated.find("\n")
                    if first_newline > 0:
                        truncated = truncated[first_newline + 1 :]
                    content = f"... (생략) ...\n{truncated}"

                reply_obj = (
                    update_or_query.message
                    if hasattr(update_or_query, "message")
                    else update_or_query
                )
                await reply_obj.reply_text(
                    f"{header}```\n{content}\n```", parse_mode="MarkdownV2"
                )
                return True
            else:
                # 전체 파일 전송
                caption = format_log_caption(dir_name)
                filename = f"{dir_name}_{log_file_path.name}"
                # [수정] target_name 전달
                return await self.send_file_async(
                    update_or_query,
                    str(log_file_path),
                    caption,
                    "document",
                    filename,
                    target_name=dir_name,
                )

        except Exception as e:
            logger.error(f"로그 전송 헬퍼 오류 ({target_path}): {e}")
            return False

    async def send_program_log(
        self, query: CallbackQuery, target: str, tail: Optional[int] = None
    ) -> None:
        """
        개별 프로그램의 monitor.log 파일 전송

        Args:
            query: CallbackQuery 객체
            target: 디렉토리 이름
            tail: 마지막 N줄만 전송 (None이면 전체)
        """
        try:
            target_path = self.process_controller.find_target_directory(target)
            if not target_path:
                await query.message.reply_text("❌ 디렉토리를 찾을 수 없습니다.")
                return

            # 로그 파일 존재 여부 확인 (헬퍼 호출 전 간단 체크)
            log_file_path = self._resolve_log_file_path(target_path)
            if log_file_path is None:
                await query.message.reply_text("⚠️ 로그 파일이 없거나 비어있습니다.")
                return

            # [수정] 대기 메시지는 send_file_async에서 처리하므로 여기서는 간단한 토스트 메시지만
            try:
                await query.answer("로그 전송을 시작합니다...")
            except Exception:
                pass  # 라우터에서 이미 answer() 소비된 경우 무시

            success = await self.send_log_helper(query, target_path, tail)

            if not success:
                # 헬퍼가 실패했거나 내용이 없을 경우
                try:
                    await query.message.reply_text(
                        f"⚠️ {target} 로그 전송에 실패했습니다."
                    )
                except:
                    pass

        except Exception as e:
            logger.error(f"로그 전송 오류: {e}")
            try:
                await query.message.reply_text(f"❌ 로그 전송 오류: {e}")
            except Exception:
                pass

    async def send_main_bot_log(
        self, query: CallbackQuery, tail: Optional[int] = None
    ) -> None:
        """
        메인 봇(Z-Pulse)의 로그 파일 전송

        Args:
            query: CallbackQuery 객체
            tail: 마지막 N줄만 전송 (None이면 전체)
        """
        try:
            log_path = Path(__file__).resolve().parent.parent / "z_pulse.log"

            if (
                not log_path.exists()
                or not log_path.is_file()
                or log_path.stat().st_size == 0
            ):
                await query.answer("⚠️ 메인봇 로그 파일이 없거나 비어있습니다.")
                return

            await query.answer("로그 전송을 시작합니다...")

            if tail:

                def _read_tail():
                    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                        lines = deque(f, tail)
                    return "".join(lines)

                loop = asyncio.get_running_loop()
                content = await loop.run_in_executor(None, _read_tail)
                content = strip_ansi(content)

                if not content.strip():
                    await query.message.reply_text("⚠️ 메인봇 로그 내용이 없습니다.")
                    return

                header = format_log_caption("telegram-bot", tail_lines=tail)
                max_content_len = 4096 - len(header) - 10

                if len(content) > max_content_len:
                    truncated = content[-(max_content_len - 20) :]
                    first_newline = truncated.find("\n")
                    if first_newline > 0:
                        truncated = truncated[first_newline + 1 :]
                    content = f"... (생략) ...\n{truncated}"

                reply_obj = query.message
                await reply_obj.reply_text(
                    f"{header}```\n{content}\n```", parse_mode="MarkdownV2"
                )
            else:
                caption = format_log_caption("telegram-bot")
                success = await self.send_file_async(
                    query,
                    str(log_path),
                    caption,
                    "document",
                    "z_pulse.log",
                    target_name="telegram-bot",
                )
                if not success:
                    try:
                        await query.message.reply_text(
                            "❌ 메인봇 로그 전송에 실패했습니다."
                        )
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"메인봇 로그 전송 오류: {e}")
            try:
                await query.answer("❌ 오류 발생")
            except Exception:
                pass

    def delete_files_in_dir(self, dir_path: Path, endswith_pattern: str) -> List[str]:
        """
        특정 디렉토리에서 패턴으로 끝나는 파일 삭제

        Args:
            dir_path: 대상 디렉토리 경로
            endswith_pattern: 파일명 끝 패턴 (예: '.log', '_backup.json')

        Returns:
            삭제된 파일 목록
        """
        deleted_files = []
        if not dir_path.exists():
            return deleted_files

        for f in os.listdir(dir_path):
            if f.endswith(endswith_pattern):
                try:
                    os.remove(os.path.join(dir_path, f))
                    deleted_files.append(f)
                except Exception as e:
                    logger.error(f"파일 삭제 실패 ({f}): {e}")
        return deleted_files
