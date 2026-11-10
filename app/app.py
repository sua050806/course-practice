import os
import json
import redis
from flask import Flask, jsonify

app = Flask(__name__)

# Redis 연결 설정
# docker-compose.yml의 environment에서 REDIS_HOST=redis로 넘겨받음
# 로컬에서 Flask만 따로 실행할 때는 localhost를 사용
redis_host = os.environ.get("REDIS_HOST", "localhost")

# decode_responses=True: Redis에서 꺼낸 값을 bytes가 아니라 문자열로 받음
redis_client = redis.Redis(host=redis_host, port=6379, decode_responses=True)

# courses.json 파일 경로
# app.py와 같은 폴더에 있는 courses.json을 가리킴
COURSES_PATH = os.path.join(os.path.dirname(__file__), "courses.json")


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
        item = {}
        item["id"] = course["id"]
        item["name"] = course["name"]
        item["credit"] = course["credit"]
        item["capacity"] = course["capacity"]
        item["times"] = course["times"]
        # 잔여 정원은 파일이 아니라 Redis에서 가져옴
        item["remaining"] = get_remaining(course)
        result.append(item)
    return jsonify(result)