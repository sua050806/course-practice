import os
import json
import random
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

# courses.json 파일 경로
COURSES_PATH = os.path.join(os.path.dirname(__file__), "courses.json")

# 시간표 추천에 쓰는 상수
VALID_DAYS = ["월", "화", "수", "목", "금"]
MORNING_END_PERIOD = 3      # 1~3교시를 오전 수업으로 봄
MIN_TARGET_CREDIT = 2       # 추천 가능한 최소 학점
MAX_TARGET_CREDIT = 21      # 추천 가능한 최대 학점
MAX_RECOMMEND_COUNT = 3     # 한 번에 돌려줄 조합 개수


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


def get_remaining(course):
    # 잔여 정원은 신청할 때마다 바뀌는 값이라 Redis에 저장
    # 키 이름 예시: remaining:CS101
    key = "remaining:" + course["id"]
    value = redis_client.get(key)

    # 처음 조회하는 과목이라 Redis에 값이 없으면 정원으로 초기화
    # setnx는 "키가 없을 때만 저장"하는 명령
    # 워커 여러 개가 동시에 초기화해도 한 번만 저장되고,
    # 이미 신청이 진행된 값을 정원으로 되돌리지 않음
    if value is None:
        redis_client.setnx(key, course["capacity"])
        value = redis_client.get(key)

    return int(value)


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
    for time in course["times"]:
        # 공강으로 지정한 요일에 수업이 있으면 제외
        if time["day"] in off_days:
            return False
        # 오전 제외를 선택했는데 오전 교시에 수업이 시작하면 제외
        if no_morning:
            if time["start"] <= MORNING_END_PERIOD:
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


@app.route("/api/health")
def health():
    # 서버 상태 확인용 API
    # Redis 연결 여부와 요청을 처리한 워커 번호를 함께 반환
    redis_ok = False
    try:
        redis_client.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    result = {}
    result["status"] = "ok"
    result["redis"] = redis_ok
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