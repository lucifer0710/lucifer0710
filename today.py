import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib
import xml.etree.ElementTree as ET

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
HEADERS = {'authorization': 'token ' + os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}
OWNER_ID = None  # Initialize as None


def daily_readme(birthday):
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years),
        diff.months, 'month' + format_plural(diff.months),
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else ''
    )


def format_plural(unit):
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code == 200:
        return request
    raise Exception(func_name, 'failed with', request.status_code, request.text, QUERY_COUNT)


def graph_commits(start_date, end_date):
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date, 'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        nameWithOwner
                        stargazers { totalCount }
                    }
                }
                pageInfo { endCursor hasNextPage }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    data = request.json()['data']['user']['repositories']
    if count_type == 'repos':
        return data['totalCount']
    elif count_type == 'stars':
        return stars_counter(data['edges'])


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    committedDate
                                    author { user { id } }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo { endCursor hasNextPage }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code == 200:
        repo = request.json()['data']['repository']
        if repo and repo['defaultBranchRef'] is not None:
            return loc_counter_one_repo(owner, repo_name, data, cache_comment, repo['defaultBranchRef']['target']['history'], addition_total, deletion_total, my_commits)
        else:
            return 0, 0, 0
    force_close_file(data, cache_comment)
    if request.status_code == 403:
        raise Exception('Too many requests in a short time!')
    raise Exception('recursive_loc() failed with', request.status_code, request.text, QUERY_COUNT)


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    global OWNER_ID
    for node in history['edges']:
        if node['node']['author'] and node['node']['author']['user'] and node['node']['author']['user']['id'] == OWNER_ID:
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']
    if not history['edges'] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else:
        return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=[]):
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        nameWithOwner
                        defaultBranchRef {
                            target {
                                ... on Commit { history { totalCount } }
                            }
                        }
                    }
                }
                pageInfo { endCursor hasNextPage }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    data = request.json()['data']['user']['repositories']
    if data['pageInfo']['hasNextPage']:
        edges += data['edges']
        return loc_query(owner_affiliation, comment_size, force_cache, data['pageInfo']['endCursor'], edges)
    else:
        return cache_builder(edges + data['edges'], comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    cached = True
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    os.makedirs('cache', exist_ok=True)
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append('Comment line\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                if int(commit_count) != edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']:
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = repo_hash + ' ' + str(edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']) + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
            except (TypeError, AttributeError):
                data[index] = repo_hash + ' 0 0 0 0\n'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    try:
        with open(filename, 'r') as f:
            data = []
            if comment_size > 0:
                data = f.readlines()[:comment_size]
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append('Comment\n')
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def add_archive():
    try:
        with open('cache/repository_archive.txt', 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        return [0, 0, 0, 0, 0]
    old_data = data
    data = data[7:len(data) - 3]
    added_loc, deleted_loc, added_commits = 0, 0, 0
    contributed_repos = len(data)
    for line in data:
        repo_hash, total_commits, my_commits, *loc = line.split()
        added_loc += int(loc[0])
        deleted_loc += int(loc[1])
        if my_commits.isdigit():
            added_commits += int(my_commits)
    if len(old_data) > 0 and len(old_data[-1].split()) > 4:
        added_commits += int(old_data[-1].split()[4][:-1])
    return [added_loc, deleted_loc, added_loc - deleted_loc, added_commits, contributed_repos]


def force_close_file(data, cache_comment):
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print('Partial cache saved to', filename)


def stars_counter(data):
    return sum(node['node']['stargazers']['totalCount'] for node in data)


def find_and_replace(root, element_id, new_text):
    namespaces = {'svg': 'http://www.w3.org/2000/svg'}
    element = root.find(f".//*[@id='{element_id}']", namespaces)
    if element is None:
        element = root.find(f".//*[@id='{element_id}']")
    if element is None:
        try:
            elements = root.xpath(f"//*[@id='{element_id}']")
            if elements:
                element = elements[0]
        except:
            pass
    if element is not None:
        element.text = str(new_text)
        return True
    else:
        print(f"⚠️  Element id '{element_id}' not found in SVG")
        return False


def justify_format(root, element_id, new_text, length=0):
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    dot_map = {0: '', 1: ' ', 2: '. '}
    dot_string = dot_map.get(just_len, ' ' + ('.' * just_len) + ' ')
    find_and_replace(root, f"{element_id}_dots", dot_string)


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    if not os.path.exists(filename):
        print(f"⚠️ SVG file '{filename}' not found. Please create it first.")
        return
    try:
        tree = etree.parse(filename)
        root = tree.getroot()
        justify_format(root, 'age_data', age_data, 22)
        justify_format(root, 'commit_data', commit_data, 22)
        justify_format(root, 'star_data', star_data, 14)
        justify_format(root, 'repo_data', repo_data, 6)
        justify_format(root, 'contrib_data', contrib_data)
        justify_format(root, 'follower_data', follower_data, 10)
        justify_format(root, 'loc_data', loc_data[2], 9)
        justify_format(root, 'loc_add', loc_data[0])
        justify_format(root, 'loc_del', loc_data[1], 7)
        tree.write(filename, encoding='utf-8', xml_declaration=True)
        print(f"✅ Successfully updated {filename}")
    except Exception as e:
        print(f"❌ Error updating {filename}: {e}")


def commit_counter(comment_size):
    total_commits = 0
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        return 0
    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for line in data:
        if len(line.split()) >= 3:
            total_commits += int(line.split()[2])
    return total_commits


def user_getter(username):
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return request.json()['data']['user']['id'], request.json()['data']['user']['createdAt']


def follower_getter(username):
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers { totalCount }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    start = time.perf_counter()
    result = funct(*args)
    return result, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    print(f"{query_type:<23} {difference * 1000:.3f} ms")
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == "__main__":
    # ✅ Define safe defaults
    total_loc = repos = stars = followers = total_commits = 0
    age_data = [0, 0, 0]
    archive_repos = 0

    try:
        print('⏱ Calculation times:')
        user_id, acc_date = user_getter(USER_NAME)
        OWNER_ID = user_id

        age_data, _ = perf_counter(daily_readme, datetime.datetime(2006, 3, 9))
        total_loc, _ = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
        stars, _ = perf_counter(graph_repos_stars, 'stars', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
        repos, _ = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
        followers, _ = perf_counter(follower_getter, USER_NAME)
        commits, _ = perf_counter(graph_commits, acc_date, datetime.datetime.utcnow().isoformat())

        archive_add, archive_del, archive_net, archive_commits, archive_repos = add_archive()
        total_commits = commit_counter(0) + archive_commits
        total_loc[0] += archive_add
        total_loc[1] += archive_del
        total_loc[2] += archive_net

        print("\n📊 Final Totals:")
        print(f"Age: {age_data}")
        print(f"Repos: {repos}")
        print(f"Stars: {stars}")
        print(f"Followers: {followers}")
        print(f"Commits: {total_commits}")
        print(f"LOC Added: {total_loc[0]:,}")
        print(f"LOC Deleted: {total_loc[1]:,}")
        print(f"Net LOC: {total_loc[2]:,}")
        print(f"Archived Repos: {archive_repos}")
# --- Update the SVG file with your stats ---
update_svg(
    "darkmode.svg",
    {
        "age_data": age_data,
        "commit_data": total_commits,
        "follower_data": followers,
        "repo_data": repos,
        "star_data": stars,
        "loc_data": f"{total_loc[2]}",
        "loc_add": f"+{total_loc[0]}",
        "loc_del": f"-{total_loc[1]}",
    },
)
    except Exception as e:
        print(f"❌ Error during stats calculation: {e}")
# ✅ Corrected order of parameters


def update_svg(file_path, updates):
    tree = ET.parse(file_path)
    root = tree.getroot()

    # ✅ Fix: register SVG namespace to remove ns0 prefixes
    ET.register_namespace('', "http://www.w3.org/2000/svg")

    # Helper function to update tspan text by ID
    def set_text_by_id(element_id, text):
        elem = root.find(f".//*[@id='{element_id}']")
        if elem is not None:
            elem.text = str(text)
        else:
            print(f"⚠️ Warning: ID '{element_id}' not found in SVG")

    # Apply updates from the dictionary
    for key, value in updates.items():
        set_text_by_id(key, value)

    # ✅ Save back to same file (clean SVG, no ns0:)
    tree.write(file_path, encoding="utf-8", xml_declaration=True)
    print("✅ darkmode.svg successfully updated with latest GitHub stats!")



