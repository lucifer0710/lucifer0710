import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
# Issues and pull requests permissions not needed at the moment, but may be used in the future
HEADERS = {'authorization': 'token ' + os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_ID']
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0,
               'loc_query': 0}
OWNER_ID = None  # Initialize as None


def daily_readme(birthday):
    """
    Returns the length of time since I was born
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years),
        diff.months, 'month' + format_plural(diff.months),
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    """
    Returns a properly formatted number
    """
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    """
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables},
                            headers=HEADERS)
    if request.status_code == 200:
        return request
    raise Exception(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)


def graph_commits(start_date, end_date):
    """
    Uses GitHub's GraphQL v4 API to return my total commit count
    """
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
    """
    Uses GitHub's GraphQL v4 API to return my total repository or star count.
    """
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
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
    """
    Uses GitHub's GraphQL v4 API and cursor pagination to fetch 100 commits from a repository at a time
    """
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
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables},
                            headers=HEADERS)
    if request.status_code == 200:
        repo = request.json()['data']['repository']
        if repo and repo['defaultBranchRef'] is not None:  # Only count commits if repo isn't empty
            return loc_counter_one_repo(owner, repo_name, data, cache_comment,
                                        repo['defaultBranchRef']['target']['history'],
                                        addition_total, deletion_total, my_commits)
        else:
            return 0, 0, 0
    force_close_file(data, cache_comment)
    if request.status_code == 403:
        raise Exception(
            'Too many requests in a short amount of time!\nYou\'ve hit the non-documented anti-abuse limit!')
    raise Exception('recursive_loc() has failed with a', request.status_code, request.text, QUERY_COUNT)


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """
    Recursively call recursive_loc
    """
    global OWNER_ID
    for node in history['edges']:
        if (node['node']['author']
                and node['node']['author']['user']
                and node['node']['author']['user']['id'] == OWNER_ID):
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    if not history['edges'] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else:
        return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits,
                             history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=[]):
    """
    Query repositories and calculate LOC
    """
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
            edges {
                node {
                    ... on Repository {
                        nameWithOwner
                        defaultBranchRef {
                            target {
                                ... on Commit {
                                    history {
                                        totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
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
    """
    Caches LOC calculations
    """
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
                data.append('This line is a comment block. Write whatever you want here.\n')
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
                    data[index] = repo_hash + ' ' + str(
                        edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']) + ' ' + str(
                        loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
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
    """
    Wipes the cache file
    """
    try:
        with open(filename, 'r') as f:
            data = []
            if comment_size > 0:
                data = f.readlines()[:comment_size]
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append('This line is a comment block. Write whatever you want here.\n')

    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def add_archive():
    """
    Add deleted repo contributions
    """
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
    """
    Forces the file to close, preserving whatever data was written
    """
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print('There was an error while writing to the cache file. Partial data saved to', filename)


def stars_counter(data):
    """
    Count total stars
    """
    total_stars = 0
    for node in data:
        total_stars += node['node']['stargazers']['totalCount']
    return total_stars


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    """
    Parse SVG files and update elements
    """
    if not os.path.exists(filename):
        print(f"Warning: SVG file {filename} does not exist. Please create it first.")
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
        print(f"Successfully updated {filename}")

    except Exception as e:
        print(f"Error updating SVG file {filename}: {e}")


def justify_format(root, element_id, new_text, length=0):
    """
    Updates and formats SVG text
    """
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def format_number(num):
    """
    Format numbers with commas
    """
    if isinstance(num, (int, float)):
        return f"{num:,}"
    return str(num)


def find_and_replace(root, element_id, new_text):
    """
    Finds the element in the SVG file and replaces its text
    """
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
        print(f"Warning: Element with id '{element_id}' not found in SVG")
        return False


def commit_counter(comment_size):
    """
    Counts total commits
    """
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
    """
    Returns the account ID and creation time
    """
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
    """
    Returns number of followers
    """
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    """
    Counts how many times GitHub API is called
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """
    Calculates runtime
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Prints runtime and optionally returns formatted result
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print(
        '{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == '__main__':
    try:
        print('Calculation times:')

        user_id, acc_date = user_getter(USER_NAME)
        OWNER_ID = user_id
        user_time = time.perf_counter()
        formatter('account data', user_time)

        age_data, age_time = perf_counter(daily_readme, datetime.datetime(2006, 3, 9))
        formatter('age calculation', age_time)

        total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
        formatter('loc calculation', loc_time)

        stars, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
        formatter('star count', star_time)

        repos, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
        formatter('repo count', repo_time)

        followers, follower_time = perf_counter(follower_getter, USER_NAME)
        formatter('follower count', follower_time)

        commits, commit_time = perf_counter(graph_commits, acc_date, datetime.datetime.utcnow().isoformat())
        formatter('commit count', commit_time)

        # archived repos
        archive_add, archive_del, archive_net, archive_commits, archive_repos = add_archive()

        # total commits (cached + archived)
        total_commits = commit_counter(0) + archive_commits

        # combine LOC with archived
        total_loc[0] += archive_add
        total_loc[1] += archive_del
        total_loc[2] += archive_net

        # Output summary
        print("\nFinal Totals:")
        print(f"   Age: {age_data}")
        print(f"   Repos: {repos}")
        print(f"   Stars: {stars}")
        print(f"   Followers: {followers}")
        print(f"   Commits: {total_commits}")
        print(f"   LOC Added: {total_loc[0]:,}")
        print(f"   LOC Deleted: {total_loc[1]:,}")
        print(f"   Net LOC: {total_loc[2]:,}")
        print(f"   Archived Repos: {archive_repos}")

        # Update SVGs if present
        svg_overwrite(
            'darkmode.svg/output.svg',
            age_data,
            total_commits,
            stars,
            repos,
            commits,
            followers,
            total_loc
        )

    except Exception as e:
        print("Error:", e)
