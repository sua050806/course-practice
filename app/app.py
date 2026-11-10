import os
import json
import math
import time
import random
import socket
import redis
from flask import Flask, jsonify, request

app = Flask(__name__)

# JSON 응답에서 한글을 \uXXXX로 바꾸지 않고 그대로 출력
app.json.ensure_ascii = False

# Redis 연결 설정
# docker-compose.yml의 environment에서 REDIS_HOST=redis로 넘겨받음
# 로컬에서 Flask만 따로 실행할 때는 localhost를 사용
redis_host = os.environ.get("REDIS_HOST", "localhost")

# decode_responses=True: Redis에서 꺼낸 값을 bytes가 아니라 문자열로 받음
redis_client = redis.Redis(host=redis_host, port=6379, decode_responses=True)

# 관리자 키 (docker-compose.yml의 ADMIN_KEY 환경변수)
ADMIN_KEY = os.environ.get("ADMIN_KEY", "admin1234")

# 서버 이름: 응답에 넣어서 어느 서버가 요청을 처리했는지 표시
# SERVER_NAME 환경변수가 없으면 컨테이너 호스트명을 사용
SERVER_NAME = os.environ.get("SERVER_NAME", socket.gethostname())

# courses.json 파일 경로
COURSES_PATH = os.path.join(os.path.dirname(__file__), "courses.json")

# 시간표 추천에 쓰는 상수
VALID_DAYS = ["월", "화", "수", "목", "금"]
MORNING_END_PERIOD = 3      # 1~3교시를 오전 수업으로 봄
MIN_TARGET_CREDIT = 2       # 추천 가능한 최소 학점
MAX_TARGET_CREDIT = 21      # 추천 가능한 최대 학점
MAX_RECOMMEND_COUNT = 3     # 한 번에 돌려줄 조합 개수

# 장바구니·수강신청에 쓰는 상수
MAX_CART_CREDIT = 21        # 장바구니에 담을 수 있는 최대 학점
MAX_ENROLL_CREDIT = 21      # 수강신청 가능한 최대 학점
NICKNAME_MAX_LENGTH = 10    # 닉네임 최대 글자 수

# 신청 오픈 관련 상수
OPEN_AT_KEY = "enroll:open_at"  # 오픈 시각을 저장하는 Redis 키
MAX_OPEN_DELAY = 60             # 관리자가 설정할 수 있는 최대 오픈 대기 시간(초)


def load_courses():
    # 과목 정보(이름, 학점, 시간, 정원)를 파일에서 읽어옴
    # 바뀌지 않는 데이터라서 서버 시작 시 한 번만 읽음
    course_file = open(COURSES_PATH, "r", encoding="utf-8")
    data = json.load(course_file)
    course_file.close()
    return data


# 서버가 시작될 때 과목 목록을 메모리에 올려둠
# 워커마다 따로 읽지만 내용이 같아서 공유할 필요 없음
courses = load_courses()

# 과목 ID로 과목을 바로 찾기 위한 딕셔너리
# 예: courses_by_id["CS101"] → 웹프로그래밍 과목 정보
courses_by_id = {}
for course_item in courses:
    courses_by_id[course_item["id"]] = course_item


def get_remaining_key(course_id):
    # 잔여 정원을 저장하는 Redis 키 이름
    # 예: remaining:CS101
    return "remaining:" + course_id


def get_remaining(course):
    # 잔여 정원은 신청할 때마다 바뀌는 값이라 Redis에 저장
    key = get_remaining_key(course["id"])
    value = redis_client.get(key)

    # 처음 조회하는 과목이라 Redis에 값이 없으면 정원으로 초기화
    # setnx는 "키가 없을 때만 저장"하는 명령
    # 워커 여러 개가 동시에 초기화해도 한 번만 저장되고,
    # 이미 신청이 진행된 값을 정원으로 되돌리지 않음
    if value is None:
        redis_client.setnx(key, course["capacity"])
        value = redis_client.get(key)

    remaining = int(value)

    # 신청이 몰리는 순간 DECR 후 INCR로 되돌리기 전에 잠깐 음수가 될 수 있음
    # 화면에는 음수 대신 0으로 보여줌
    if remaining < 0:
        remaining = 0
    return remaining


def make_course_summary(course):
    # 응답에 담을 과목 정보만 골라서 새 딕셔너리로 만듦
    item = {}
    item["id"] = course["id"]
    item["name"] = course["name"]
    item["credit"] = course["credit"]
    item["times"] = course["times"]
    return item


def is_time_conflict(course_a, course_b):
    # 두 과목의 수업 시간이 하나라도 겹치면 True
    for time_a in course_a["times"]:
        for time_b in course_b["times"]:
            if time_a["day"] == time_b["day"]:
                # 같은 요일에서 교시 구간이 겹치는지 검사
                # 예: 1~2교시와 2~3교시는 2교시가 겹침
                if time_a["start"] <= time_b["end"] and time_b["start"] <= time_a["end"]:
                    return True
    return False


def fits_condition(course, off_days, no_morning):
    # 과목이 사용자의 조건(공강 요일, 오전 제외)을 만족하는지 검사
    for time_slot in course["times"]:
        # 공강으로 지정한 요일에 수업이 있으면 제외
        if time_slot["day"] in off_days:
            return False
        # 오전 제외를 선택했는데 오전 교시에 수업이 시작하면 제외
        if no_morning:
            if time_slot["start"] <= MORNING_END_PERIOD:
                return False
    return True


def search_combinations(candidates, start_index, selected, current_credit, target_credit, results):
    # 백트래킹으로 시간이 겹치지 않고 학점 합이 목표와 같은 조합을 찾음
    # candidates: 조건을 통과한 과목 목록
    # start_index: 이번에 검사를 시작할 과목 위치 (같은 조합을 중복으로 만들지 않기 위해 앞으로만 진행)
    # selected: 지금까지 고른 과목들
    # results: 찾은 조합을 담는 리스트

    # 목표 학점을 정확히 채우면 현재 조합을 복사해서 저장
    if current_credit == target_credit:
        found = []
        for course in selected:
            found.append(course)
        results.append(found)
        return

    index = start_index
    while index < len(candidates):
        # 필요한 개수만큼 찾았으면 더 탐색하지 않음
        if len(results) >= MAX_RECOMMEND_COUNT:
            return

        course = candidates[index]

        # 이 과목을 넣었을 때 목표 학점을 넘지 않는 경우만 검사
        if current_credit + course["credit"] <= target_credit:
            # 이미 고른 과목들과 시간이 겹치는지 확인
            has_conflict = False
            for chosen in selected:
                if is_time_conflict(chosen, course):
                    has_conflict = True
                    break

            if not has_conflict:
                # 과목을 넣고 다음 단계로 탐색
                selected.append(course)
                search_combinations(candidates, index + 1, selected, current_credit + course["credit"], target_credit, results)
                # 탐색이 끝나면 넣었던 과목을 빼고 다음 과목을 시도
                selected.pop()

        index = index + 1


def make_error(message, status_code):
    # 오류 응답을 같은 형식으로 만들기 위한 함수
    body = {}
    body["message"] = message
    return jsonify(body), status_code


def read_nickname(value):
    # 닉네임을 검사해서 올바르면 앞뒤 공백을 지운 닉네임을, 아니면 None을 반환
    if not isinstance(value, str):
        return None
    nickname = value.strip()
    if len(nickname) == 0:
        return None
    if len(nickname) > NICKNAME_MAX_LENGTH:
        return None
    return nickname


def get_courses_in_set(set_key):
    # Redis Set에 들어 있는 과목 ID들을 과목 정보 목록으로 바꿔서 반환
    # smembers: Set에 들어 있는 값 전체를 가져오는 명령
    course_ids = redis_client.smembers(set_key)

    # courses.json 순서대로 정렬되도록 전체 과목을 돌면서 Set에 있는 것만 고름
    result = []
    for course in courses:
        if course["id"] in course_ids:
            result.append(course)
    return result


def make_course_list_response(nickname, course_list, message):
    # 과목 목록(장바구니·신청 내역)과 총 학점을 응답 형식으로 만듦
    body = {}
    body["nickname"] = nickname
    body["totalCredit"] = 0
    body["courses"] = []
    for course in course_list:
        body["courses"].append(make_course_summary(course))
        body["totalCredit"] = body["totalCredit"] + course["credit"]
    body["message"] = message
    return jsonify(body)


# ---------------- 장바구니 ----------------

def get_cart_key(nickname):
    # 장바구니를 저장하는 Redis 키 이름
    # 예: cart:수아
    return "cart:" + nickname


def get_cart_courses(nickname):
    # 장바구니에 담긴 과목 목록
    return get_courses_in_set(get_cart_key(nickname))


# ---------------- 수강신청 ----------------

def get_enrolled_key(nickname):
    # 신청에 성공한 과목을 저장하는 Redis 키 이름
    # 예: enrolled:수아
    return "enrolled:" + nickname


def get_enrolled_courses(nickname):
    # 신청에 성공한 과목 목록
    return get_courses_in_set(get_enrolled_key(nickname))


def get_open_state():
    # 현재 신청 상태를 계산해서 반환
    # closed: 오픈 시각이 없음 (신청 불가)
    # waiting: 오픈 시각 전 (카운트다운)
    # open: 오픈 시각 후 (신청 가능)
    state = {}
    now = time.time()
    state["serverTime"] = now

    value = redis_client.get(OPEN_AT_KEY)
    if value is None:
        state["state"] = "closed"
        state["remainingSeconds"] = None
        return state

    open_at = float(value)
    remaining_seconds = open_at - now

    if remaining_seconds > 0:
        state["state"] = "waiting"
        # 남은 시간을 올림해서 정수 초로 표시 (예: 4.2초 → 5초)
        state["remainingSeconds"] = math.ceil(remaining_seconds)
    else:
        state["state"] = "open"
        state["remainingSeconds"] = 0

    return state


def make_enroll_result(result, message, status_code, course_id, remaining):
    # 수강신청 응답을 같은 형식으로 만듦
    # 성공·실패 모두 처리한 서버와 워커 번호를 넣어서 부하 테스트에서 분포를 집계할 수 있게 함
    body = {}
    body["result"] = result         # success, full, not_open, duplicate, conflict, credit_over, invalid
    body["message"] = message
    body["courseId"] = course_id
    body["remaining"] = remaining
    body["server"] = SERVER_NAME
    body["workerPid"] = os.getpid()
    return jsonify(body), status_code


# ---------------- 관리자 ----------------

def is_admin(data):
    # 요청에 담긴 관리자 키가 맞는지 확인
    if data is None:
        return False
    return data.get("adminKey") == ADMIN_KEY


def make_admin_response(message):
    # 관리자 API 응답: 메시지와 현재 신청 상태를 함께 반환
    body = get_open_state()
    body["message"] = message
    return jsonify(body)


# ================= API =================

@app.route("/api/health")
def health():
    # 서버 상태 확인용 API
    # Redis 연결 여부와 요청을 처리한 서버·워커 번호를 함께 반환
    redis_ok = False
    try:
        redis_client.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    result = {}
    result["status"] = "ok"
    result["redis"] = redis_ok
    result["server"] = SERVER_NAME
    # 새로고침할 때마다 값이 바뀌면 워커들이 요청을 나눠 처리하고 있다는 뜻
    result["worker_pid"] = os.getpid()
    return jsonify(result)


@app.route("/api/courses")
def course_list():
    # 전체 과목 목록과 각 과목의 잔여 정원을 반환
    result = []
    for course in courses:
        item = make_course_summary(course)
        item["capacity"] = course["capacity"]
        # 잔여 정원은 파일이 아니라 Redis에서 가져옴
        item["remaining"] = get_remaining(course)
        result.append(item)
    return jsonify(result)


@app.route("/api/recommend", methods=["POST"])
def recommend():
    # 조건을 받아서 시간표 조합을 추천하는 API
    # 요청 예시: {"targetCredit": 15, "offDays": ["금"], "noMorning": true}

    # 1. 요청 본문 읽기
    data = request.get_json(silent=True)
    if data is None:
        return make_error("요청 형식이 올바르지 않습니다.", 400)

    # 2. 목표 학점 검사 (경계 케이스: 숫자가 아니거나 범위를 벗어남)
    target_credit = data.get("targetCredit")
    if not isinstance(target_credit, int) or isinstance(target_credit, bool):
        return make_error("목표 학점은 숫자로 입력해야 합니다.", 400)
    if target_credit < MIN_TARGET_CREDIT or target_credit > MAX_TARGET_CREDIT:
        return make_error("목표 학점은 2학점 이상 21학점 이하로 입력해야 합니다.", 400)

    # 3. 공강 요일 검사 (월~금 중에서만 선택 가능)
    off_days = data.get("offDays")
    if off_days is None:
        off_days = []
    if not isinstance(off_days, list):
        return make_error("공강 요일 형식이 올바르지 않습니다.", 400)
    for day in off_days:
        if day not in VALID_DAYS:
            return make_error("공강 요일은 월~금 중에서 선택해야 합니다.", 400)

    # 4. 오전 수업 제외 여부
    no_morning = False
    if data.get("noMorning") is True:
        no_morning = True

    # 5. 조건을 만족하는 과목만 후보로 남김
    candidates = []
    for course in courses:
        if fits_condition(course, off_days, no_morning):
            candidates.append(course)

    # 6. 후보 순서를 섞어서 추천할 때마다 다른 조합이 나오게 함
    random.shuffle(candidates)

    # 7. 백트래킹으로 조합 탐색
    found_combinations = []
    search_combinations(candidates, 0, [], 0, target_credit, found_combinations)

    # 8. 응답 만들기
    result = {}
    result["count"] = len(found_combinations)
    result["combinations"] = []

    for combination in found_combinations:
        combination_item = {}
        combination_item["totalCredit"] = 0
        combination_item["courses"] = []
        for course in combination:
            combination_item["courses"].append(make_course_summary(course))
            combination_item["totalCredit"] = combination_item["totalCredit"] + course["credit"]
        result["combinations"].append(combination_item)

    # 실패 케이스: 조건을 만족하는 조합이 하나도 없음
    if len(found_combinations) == 0:
        result["message"] = "조건을 만족하는 시간표가 없습니다. 학점이나 공강 조건을 바꿔보세요."
    else:
        result["message"] = "추천 시간표 " + str(len(found_combinations)) + "개를 찾았습니다."

    return jsonify(result)


@app.route("/api/cart", methods=["GET"])
def cart_get():
    # 장바구니 조회 API
    # 요청 예시: GET /api/cart?nickname=수아
    nickname = read_nickname(request.args.get("nickname"))
    if nickname is None:
        return make_error("닉네임은 1자 이상 10자 이하로 입력해야 합니다.", 400)

    return make_course_list_response(nickname, get_cart_courses(nickname), "장바구니를 불러왔습니다.")


@app.route("/api/cart", methods=["POST"])
def cart_add():
    # 장바구니 담기 API
    # 요청 예시: {"nickname": "수아", "courseId": "CS101"}

    # 1. 요청 본문 읽기
    data = request.get_json(silent=True)
    if data is None:
        return make_error("요청 형식이 올바르지 않습니다.", 400)

    # 2. 닉네임 검사
    nickname = read_nickname(data.get("nickname"))
    if nickname is None:
        return make_error("닉네임은 1자 이상 10자 이하로 입력해야 합니다.", 400)

    # 3. 과목 ID 검사 (courses.json에 없는 과목)
    course_id = data.get("courseId")
    if course_id not in courses_by_id:
        return make_error("존재하지 않는 과목입니다.", 404)
    course = courses_by_id[course_id]

    cart_key = get_cart_key(nickname)

    # 4. 이미 담은 과목인지 확인
    # sismember: Set 안에 값이 있는지 확인하는 명령
    if redis_client.sismember(cart_key, course_id):
        return make_error("이미 장바구니에 담은 과목입니다.", 409)

    # 5. 장바구니에 있는 과목들과 시간이 겹치는지 확인
    cart_courses = get_cart_courses(nickname)
    total_credit = 0
    for cart_course in cart_courses:
        total_credit = total_credit + cart_course["credit"]
        if is_time_conflict(cart_course, course):
            return make_error(cart_course["name"] + "과(와) 시간이 겹칩니다.", 409)

    # 6. 담았을 때 최대 학점을 넘는지 확인
    if total_credit + course["credit"] > MAX_CART_CREDIT:
        return make_error("장바구니는 최대 21학점까지 담을 수 있습니다.", 400)

    # 7. 모든 검사를 통과하면 Set에 과목 ID 추가
    redis_client.sadd(cart_key, course_id)

    return make_course_list_response(nickname, get_cart_courses(nickname), course["name"] + "을(를) 장바구니에 담았습니다.")


@app.route("/api/cart", methods=["DELETE"])
def cart_remove():
    # 장바구니 빼기 API
    # 요청 예시: {"nickname": "수아", "courseId": "CS101"}

    # 1. 요청 본문 읽기
    data = request.get_json(silent=True)
    if data is None:
        return make_error("요청 형식이 올바르지 않습니다.", 400)

    # 2. 닉네임 검사
    nickname = read_nickname(data.get("nickname"))
    if nickname is None:
        return make_error("닉네임은 1자 이상 10자 이하로 입력해야 합니다.", 400)

    # 3. 과목 ID 검사
    course_id = data.get("courseId")
    if course_id not in courses_by_id:
        return make_error("존재하지 않는 과목입니다.", 404)
    course = courses_by_id[course_id]

    cart_key = get_cart_key(nickname)

    # 4. 장바구니에 없는 과목을 빼려고 하면 거부
    if not redis_client.sismember(cart_key, course_id):
        return make_error("장바구니에 없는 과목입니다.", 404)

    # 5. Set에서 과목 ID 제거
    # srem: Set에서 값을 빼는 명령
    redis_client.srem(cart_key, course_id)

    return make_course_list_response(nickname, get_cart_courses(nickname), course["name"] + "을(를) 장바구니에서 뺐습니다.")


@app.route("/api/status", methods=["GET"])
def enroll_status():
    # 신청 상태 조회 API (신청 화면이 1초마다 호출)
    return jsonify(get_open_state())


@app.route("/api/enroll", methods=["POST"])
def enroll():
    # 수강신청 API — 신청이 몰리는 핵심 기능
    # 요청 예시: {"nickname": "수아", "courseId": "CS101"}

    # 1. 요청 본문 읽기
    data = request.get_json(silent=True)
    if data is None:
        return make_enroll_result("invalid", "요청 형식이 올바르지 않습니다.", 400, None, None)

    # 2. 닉네임 검사
    nickname = read_nickname(data.get("nickname"))
    if nickname is None:
        return make_enroll_result("invalid", "닉네임은 1자 이상 10자 이하로 입력해야 합니다.", 400, None, None)

    # 3. 과목 ID 검사
    course_id = data.get("courseId")
    if course_id not in courses_by_id:
        return make_enroll_result("invalid", "존재하지 않는 과목입니다.", 404, None, None)
    course = courses_by_id[course_id]

    # 4. 신청 기간인지 확인 (오픈 전·마감 후에는 거부)
    state = get_open_state()
    if state["state"] != "open":
        return make_enroll_result("not_open", "아직 신청 기간이 아닙니다.", 403, course_id, None)

    enrolled_key = get_enrolled_key(nickname)

    # 5. 이미 신청한 과목인지 확인
    if redis_client.sismember(enrolled_key, course_id):
        return make_enroll_result("duplicate", "이미 신청한 과목입니다.", 409, course_id, None)

    # 6. 신청한 과목들과 시간이 겹치는지 확인
    enrolled_courses = get_enrolled_courses(nickname)
    total_credit = 0
    for enrolled_course in enrolled_courses:
        total_credit = total_credit + enrolled_course["credit"]
        if is_time_conflict(enrolled_course, course):
            return make_enroll_result("conflict", enrolled_course["name"] + "과(와) 시간이 겹칩니다.", 409, course_id, None)

    # 7. 신청하면 최대 학점을 넘는지 확인
    if total_credit + course["credit"] > MAX_ENROLL_CREDIT:
        return make_enroll_result("credit_over", "최대 21학점까지 신청할 수 있습니다.", 400, course_id, None)

    # 8. 잔여 정원 키가 없으면 정원으로 초기화
    get_remaining(course)
    remaining_key = get_remaining_key(course_id)

    # 9. 정원 1 빼기 — 몰림 처리의 핵심
    # DECR은 "1 빼고 바뀐 값을 돌려주기"를 한 번에 처리함
    # Redis는 명령을 하나씩 순서대로 처리하므로 워커 여러 개가 동시에 요청해도 결과값이 겹치지 않음
    new_remaining = redis_client.decr(remaining_key)

    # 10. 뺀 결과가 0보다 작으면 이미 정원이 찬 상태 → 뺀 1을 되돌리고 마감 처리
    if new_remaining < 0:
        redis_client.incr(remaining_key)
        return make_enroll_result("full", "정원이 마감되었습니다.", 409, course_id, 0)

    # 11. 신청 내역에 추가
    # sadd는 새로 추가되면 1, 이미 있으면 0을 돌려줌
    # 같은 사람이 버튼을 아주 빠르게 두 번 눌러서 5번 검사를 동시에 통과한 경우를 막기 위한 확인
    added = redis_client.sadd(enrolled_key, course_id)
    if added == 0:
        redis_client.incr(remaining_key)
        return make_enroll_result("duplicate", "이미 신청한 과목입니다.", 409, course_id, None)

    return make_enroll_result("success", course["name"] + " 신청이 완료되었습니다.", 200, course_id, new_remaining)


@app.route("/api/my", methods=["GET"])
def my_enrollments():
    # 신청 내역 조회 API
    # 요청 예시: GET /api/my?nickname=수아
    nickname = read_nickname(request.args.get("nickname"))
    if nickname is None:
        return make_error("닉네임은 1자 이상 10자 이하로 입력해야 합니다.", 400)

    enrolled_courses = get_enrolled_courses(nickname)
    if len(enrolled_courses) == 0:
        message = "신청한 과목이 없습니다."
    else:
        message = "신청 내역을 불러왔습니다."
    return make_course_list_response(nickname, enrolled_courses, message)


@app.route("/api/admin/open", methods=["POST"])
def admin_open():
    # 관리자: N초 후 신청 오픈
    # 요청 예시: {"adminKey": "admin1234", "delaySeconds": 10}
    data = request.get_json(silent=True)
    if not is_admin(data):
        return make_error("관리자 키가 올바르지 않습니다.", 403)

    delay = data.get("delaySeconds")
    if not isinstance(delay, int) or isinstance(delay, bool):
        return make_error("대기 시간은 숫자로 입력해야 합니다.", 400)
    if delay < 0 or delay > MAX_OPEN_DELAY:
        return make_error("대기 시간은 0초 이상 60초 이하로 입력해야 합니다.", 400)

    # 오픈 시각 = 지금 + 대기 시간
    open_at = time.time() + delay
    redis_client.set(OPEN_AT_KEY, open_at)

    return make_admin_response(str(delay) + "초 후 신청이 열립니다.")


@app.route("/api/admin/close", methods=["POST"])
def admin_close():
    # 관리자: 신청 마감 (오픈 시각 삭제)
    data = request.get_json(silent=True)
    if not is_admin(data):
        return make_error("관리자 키가 올바르지 않습니다.", 403)

    redis_client.delete(OPEN_AT_KEY)
    return make_admin_response("신청을 마감했습니다.")


@app.route("/api/admin/verify", methods=["POST"])
def admin_verify():
    # 관리자: 키가 맞는지만 확인 (아무것도 바꾸지 않음)
    # 관리자 화면에서 [확인]을 누르면 호출해서, 맞으면 관리 기능 잠금을 해제함
    # 요청 예시: {"adminKey": "admin1234"}
    data = request.get_json(silent=True)
    if not is_admin(data):
        return make_error("관리자 키가 올바르지 않습니다.", 403)

    return make_admin_response("관리자 인증 완료")

@app.route("/api/admin/reset", methods=["POST"])
def admin_reset():
    # 관리자: 연습을 처음부터 다시 할 수 있도록 초기화
    # 잔여 정원을 원래 정원으로, 모든 신청 내역 삭제, 마감 상태로 변경 (장바구니는 유지)
    data = request.get_json(silent=True)
    if not is_admin(data):
        return make_error("관리자 키가 올바르지 않습니다.", 403)

    # 1. 마감 상태로
    redis_client.delete(OPEN_AT_KEY)

    # 2. 모든 과목의 잔여 정원을 원래 정원으로
    for course in courses:
        redis_client.set(get_remaining_key(course["id"]), course["capacity"])

    # 3. 모든 신청 내역 삭제
    # scan_iter: 이름이 enrolled:로 시작하는 키를 조금씩 나눠서 찾아줌
    deleted_count = 0
    for key in redis_client.scan_iter("enrolled:*"):
        redis_client.delete(key)
        deleted_count = deleted_count + 1

    return make_admin_response("초기화했습니다. 신청 내역 " + str(deleted_count) + "명분을 삭제했습니다.")